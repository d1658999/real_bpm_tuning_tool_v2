use num_complex::Complex64;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::prelude::*;
use rayon::prelude::*;

/// Apply shunt termination to port `port_k` of an N-port S-matrix for all frequencies.
///
/// Formula (rank-1 update):
///   S'[i,j] = S[i,j] + S[i,k] * Γ * S[k,j] / (1 - S[k,k] * Γ)
///   where i,j run over all ports EXCEPT k, with index remapping i -> i+(i>=k).
///
/// # Arguments
/// * `s`      - flat (nfreq * n * n) complex array, row-major
/// * `n`      - current number of ports
/// * `nfreq`  - number of frequency points
/// * `port_k` - 0-based port index to terminate (always 2 in our scheme)
/// * `gamma`  - slice of length nfreq, reflection coefficient per frequency
///
/// # Returns
/// Flat (nfreq * (n-1) * (n-1)) complex array
fn apply_termination(
    s: &[Complex64],
    n: usize,
    nfreq: usize,
    port_k: usize,
    gamma: &[Complex64],
) -> Vec<Complex64> {
    let n1 = n - 1;
    let mut result = vec![Complex64::new(0.0, 0.0); nfreq * n1 * n1];

    for f in 0..nfreq {
        let s_kk = s[f * n * n + port_k * n + port_k];
        let g = gamma[f];
        let denom = Complex64::new(1.0, 0.0) - s_kk * g;

        for i in 0..n1 {
            let ii = if i >= port_k { i + 1 } else { i };
            for j in 0..n1 {
                let jj = if j >= port_k { j + 1 } else { j };
                let s_ij  = s[f * n * n + ii * n + jj];
                let s_ik  = s[f * n * n + ii * n + port_k];
                let s_kj  = s[f * n * n + port_k * n + jj];
                result[f * n1 * n1 + i * n1 + j] = s_ij + s_ik * g * s_kj / denom;
            }
        }
    }
    result
}

/// Evaluate N-port metrics from a flat (nfreq * n * n) complex S-matrix.
/// Antenna port = port index n-1 (last signal port).
///
/// signal_eval_ranges: slice of (start_idx, stop_idx) per signal port (0-indexed).
/// Length must be >= n. Each non-antenna port i is evaluated only over signal_eval_ranges[i].
/// Antenna port (n-1) is evaluated over signal_eval_ranges[n-1] (= union of all bands).
///
/// Returns:
///   (vswr_non_ant_max, vswr_ant_max, worst_il_db)
/// where:
///   vswr_non_ant_max = max VSWR across ports 0..n-2, each over its own band
///   vswr_ant_max     = VSWR at port n-1 (antenna), over antenna union band
///   worst_il_db      = worst IL S[ant→signal_i] for any port i, over port i's band
fn compute_metrics_n(
    s: &[Complex64],
    nfreq: usize,
    n: usize,
    signal_eval_ranges: &[(usize, usize)],
) -> (f64, f64, f64) {
    let mut vswr_non_ant_max = 1.0_f64;
    let mut vswr_ant_max = 1.0_f64;
    let mut worst_il_db = 0.0_f64;
    let ant = n - 1;

    // Non-antenna signal ports: each evaluated over its own freq band
    for i in 0..ant {
        let (start, stop) = if i < signal_eval_ranges.len() {
            signal_eval_ranges[i]
        } else {
            (0, nfreq.saturating_sub(1))
        };
        for f in start..=stop.min(nfreq.saturating_sub(1)) {
            let s_ii = s[f * n * n + i * n + i].norm().min(0.99999);
            let v = (1.0 + s_ii) / (1.0 - s_ii);
            if v > vswr_non_ant_max { vswr_non_ant_max = v; }

            // IL from this signal port to antenna
            let s_ant_i = s[f * n * n + ant * n + i].norm().max(1e-15_f64);
            let il = 20.0 * s_ant_i.log10();
            if il < worst_il_db { worst_il_db = il; }
        }
    }

    // Antenna port: evaluated over its union band (last entry in signal_eval_ranges)
    let (ant_start, ant_stop) = if ant < signal_eval_ranges.len() {
        signal_eval_ranges[ant]
    } else {
        (0, nfreq.saturating_sub(1))
    };
    for f in ant_start..=ant_stop.min(nfreq.saturating_sub(1)) {
        let s_ant_ant = s[f * n * n + ant * n + ant].norm().min(0.99999);
        let v_ant = (1.0 + s_ant_ant) / (1.0 - s_ant_ant);
        if v_ant > vswr_ant_max { vswr_ant_max = v_ant; }
    }

    (vswr_non_ant_max, vswr_ant_max, worst_il_db)
}

/// Evaluate N-port metrics plus target-distance metrics from a flat S-matrix.
///
/// `target_gamma` is indexed as [signal_port][frequency] and defaults should be
/// encoded by the caller as gamma=0 for 50+0j ohm.
fn compute_metrics_n_with_targets(
    s: &[Complex64],
    nfreq: usize,
    n: usize,
    signal_eval_ranges: &[(usize, usize)],
    target_gamma: &[Vec<Complex64>],
) -> (f64, f64, f64, f64, f64) {
    let mut vswr_non_ant_max = 1.0_f64;
    let mut vswr_ant_max = 1.0_f64;
    let mut worst_il_db = 0.0_f64;
    let mut target_non_ant_max = 0.0_f64;
    let mut target_ant_max = 0.0_f64;
    let ant = n - 1;

    for i in 0..ant {
        let (start, stop) = if i < signal_eval_ranges.len() {
            signal_eval_ranges[i]
        } else {
            (0, nfreq.saturating_sub(1))
        };
        for f in start..=stop.min(nfreq.saturating_sub(1)) {
            let sii = s[f * n * n + i * n + i];
            let s_ii_mag = sii.norm().min(0.99999);
            let v = (1.0 + s_ii_mag) / (1.0 - s_ii_mag);
            if v > vswr_non_ant_max { vswr_non_ant_max = v; }

            let target = target_gamma
                .get(i)
                .and_then(|row| row.get(f))
                .copied()
                .unwrap_or_else(|| Complex64::new(0.0, 0.0));
            let target_err = (sii - target).norm();
            if target_err > target_non_ant_max { target_non_ant_max = target_err; }

            let s_ant_i = s[f * n * n + ant * n + i].norm().max(1e-15_f64);
            let il = 20.0 * s_ant_i.log10();
            if il < worst_il_db { worst_il_db = il; }
        }
    }

    let (ant_start, ant_stop) = if ant < signal_eval_ranges.len() {
        signal_eval_ranges[ant]
    } else {
        (0, nfreq.saturating_sub(1))
    };
    for f in ant_start..=ant_stop.min(nfreq.saturating_sub(1)) {
        let saa = s[f * n * n + ant * n + ant];
        let s_ant_ant = saa.norm().min(0.99999);
        let v_ant = (1.0 + s_ant_ant) / (1.0 - s_ant_ant);
        if v_ant > vswr_ant_max { vswr_ant_max = v_ant; }

        let target = target_gamma
            .get(ant)
            .and_then(|row| row.get(f))
            .copied()
            .unwrap_or_else(|| Complex64::new(0.0, 0.0));
        let target_err = (saa - target).norm();
        if target_err > target_ant_max { target_ant_max = target_err; }
    }

    (
        vswr_non_ant_max,
        vswr_ant_max,
        worst_il_db,
        target_non_ant_max,
        target_ant_max,
    )
}

/// Build all combination index arrays for n_ports ports with counts[i] candidates each.
fn build_combos(counts: &[usize]) -> Vec<Vec<usize>> {
    let total: usize = counts.iter().product();
    let mut combos = Vec::with_capacity(total);
    let mut combo = vec![0usize; counts.len()];
    for _ in 0..total {
        combos.push(combo.clone());
        // Increment last index, carry over
        for p in (0..counts.len()).rev() {
            combo[p] += 1;
            if combo[p] < counts[p] {
                break;
            }
            combo[p] = 0;
        }
    }
    combos
}

/// Main sweep function exposed to Python.
///
/// # Arguments
/// * `base_s_re` / `base_s_im`  - (nfreq, N, N) float64 base S-matrix (re/im split)
/// * `term_gammas_re` / `_im`   - list of (n_cands, nfreq) float64 per tunable port.
///                                Row 0 = open (Γ=+1 re=1, im=0).
///                                Rows 1..n_cands = actual component gammas.
/// * `eval_ranges`               - Vec of (start_idx, stop_idx) per signal port (length = n_signals). Last entry = antenna union band.
/// * `n_signals`                 - number of signal ports (2, 3, or 4); tunable ports
///                                start at index n_signals
///
/// # Returns
/// Tuple of four numpy arrays:
/// * vswr_non_ant_max : (n_combos,) float64 — max VSWR across non-antenna signal ports
/// * vswr_ant_max     : (n_combos,) float64 — VSWR at antenna port (last signal port)
/// * worst_il_db      : (n_combos,) float64
/// * combo_indices    : (n_combos, n_tunable) int32 — which candidate row was used per port
#[pyfunction]
fn sweep_terminations_parallel<'py>(
    py: Python<'py>,
    base_s_re: PyReadonlyArray3<'py, f64>,
    base_s_im: PyReadonlyArray3<'py, f64>,
    term_gammas_re: Vec<PyReadonlyArray2<'py, f64>>,
    term_gammas_im: Vec<PyReadonlyArray2<'py, f64>>,
    eval_ranges: Vec<(usize, usize)>,  // per-signal (start_idx, stop_idx); length = n_signals
    n_signals: usize,
) -> PyResult<(
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray2<i32>>,
)> {
    let base_re = base_s_re.as_array();
    let base_im = base_s_im.as_array();

    let shape = base_re.shape();
    let nfreq = shape[0];
    let n_ports = shape[1]; // N (includes s1, s2, and all tunable ports)

    let n_tunable = term_gammas_re.len();
    assert_eq!(n_tunable, n_ports - n_signals, "Expected n_ports - n_signals tunable ports");

    // Convert base S-matrix to flat Vec<Complex64>
    let base_s: Vec<Complex64> = (0..nfreq)
        .flat_map(|f| {
            (0..n_ports).flat_map(move |i| {
                (0..n_ports).map(move |j| {
                    Complex64::new(base_re[[f, i, j]], base_im[[f, i, j]])
                })
            })
        })
        .collect();

    // Convert termination gammas to Vec<Vec<Vec<Complex64>>>
    // Layout per port: [n_cands][nfreq]
    let mut all_gammas: Vec<Vec<Vec<Complex64>>> = Vec::with_capacity(n_tunable);
    let mut counts: Vec<usize> = Vec::with_capacity(n_tunable);

    for p in 0..n_tunable {
        let gre = term_gammas_re[p].as_array();
        let gim = term_gammas_im[p].as_array();
        let n_cands = gre.shape()[0];
        counts.push(n_cands);

        let mut port_gammas: Vec<Vec<Complex64>> = Vec::with_capacity(n_cands);
        for c in 0..n_cands {
            let gamma: Vec<Complex64> = (0..nfreq)
                .map(|f| Complex64::new(gre[[c, f]], gim[[c, f]]))
                .collect();
            port_gammas.push(gamma);
        }
        all_gammas.push(port_gammas);
    }

    // Build all combination indices
    let combos = build_combos(&counts);
    let n_combos = combos.len();

    // Parallel sweep using rayon
    let results: Vec<(f64, f64, f64)> = combos
        .par_iter()
        .map(|combo_indices| {
            let mut s = base_s.clone();
            let mut current_n = n_ports;

            // Tunable ports start at index n_signals; after each termination the next
            // tunable port shifts down to n_signals again as previous ports are eliminated.
            for (p, &cand_idx) in combo_indices.iter().enumerate() {
                let gamma = &all_gammas[p][cand_idx];
                s = apply_termination(&s, current_n, nfreq, n_signals, gamma);
                current_n -= 1;
            }

            // s is now (nfreq * n_signals * n_signals)
            compute_metrics_n(&s, nfreq, n_signals, &eval_ranges)
        })
        .collect();

    // Unpack results into separate arrays
    let mut vswr_non_ant = Vec::with_capacity(n_combos);
    let mut vswr_ant = Vec::with_capacity(n_combos);
    let mut worst_il = Vec::with_capacity(n_combos);
    let mut combo_idx_flat: Vec<i32> = Vec::with_capacity(n_combos * n_tunable);

    for (i, (v_non_ant, v_ant, il)) in results.iter().enumerate() {
        vswr_non_ant.push(*v_non_ant);
        vswr_ant.push(*v_ant);
        worst_il.push(*il);
        for &ci in &combos[i] {
            combo_idx_flat.push(ci as i32);
        }
    }

    let vswr_s11_arr = PyArray1::from_vec_bound(py, vswr_non_ant);
    let vswr_s22_arr = PyArray1::from_vec_bound(py, vswr_ant);
    let worst_il_arr = PyArray1::from_vec_bound(py, worst_il);

    use numpy::ndarray::Array2;
    let arr2 = Array2::from_shape_vec(
        (n_combos, n_tunable),
        combo_idx_flat,
    ).unwrap();
    let combo_idx_arr = arr2.into_pyarray_bound(py);

    Ok((vswr_s11_arr, vswr_s22_arr, worst_il_arr, combo_idx_arr))
}

/// Target-aware sweep function. In addition to the legacy VSWR and IL metrics,
/// this returns max |Sii - target_gamma| for non-antenna signal ports and for
/// the antenna/common port.
#[pyfunction]
fn sweep_terminations_parallel_targets<'py>(
    py: Python<'py>,
    base_s_re: PyReadonlyArray3<'py, f64>,
    base_s_im: PyReadonlyArray3<'py, f64>,
    term_gammas_re: Vec<PyReadonlyArray2<'py, f64>>,
    term_gammas_im: Vec<PyReadonlyArray2<'py, f64>>,
    eval_ranges: Vec<(usize, usize)>,
    n_signals: usize,
    target_gamma_re: PyReadonlyArray2<'py, f64>,
    target_gamma_im: PyReadonlyArray2<'py, f64>,
) -> PyResult<(
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray2<i32>>,
)> {
    let base_re = base_s_re.as_array();
    let base_im = base_s_im.as_array();

    let shape = base_re.shape();
    let nfreq = shape[0];
    let n_ports = shape[1];

    let n_tunable = term_gammas_re.len();
    assert_eq!(n_tunable, n_ports - n_signals, "Expected n_ports - n_signals tunable ports");

    let target_re = target_gamma_re.as_array();
    let target_im = target_gamma_im.as_array();
    let target_gamma: Vec<Vec<Complex64>> = (0..n_signals)
        .map(|i| {
            (0..nfreq)
                .map(|f| Complex64::new(target_re[[i, f]], target_im[[i, f]]))
                .collect()
        })
        .collect();

    let base_s: Vec<Complex64> = (0..nfreq)
        .flat_map(|f| {
            (0..n_ports).flat_map(move |i| {
                (0..n_ports).map(move |j| {
                    Complex64::new(base_re[[f, i, j]], base_im[[f, i, j]])
                })
            })
        })
        .collect();

    let mut all_gammas: Vec<Vec<Vec<Complex64>>> = Vec::with_capacity(n_tunable);
    let mut counts: Vec<usize> = Vec::with_capacity(n_tunable);

    for p in 0..n_tunable {
        let gre = term_gammas_re[p].as_array();
        let gim = term_gammas_im[p].as_array();
        let n_cands = gre.shape()[0];
        counts.push(n_cands);

        let mut port_gammas: Vec<Vec<Complex64>> = Vec::with_capacity(n_cands);
        for c in 0..n_cands {
            let gamma: Vec<Complex64> = (0..nfreq)
                .map(|f| Complex64::new(gre[[c, f]], gim[[c, f]]))
                .collect();
            port_gammas.push(gamma);
        }
        all_gammas.push(port_gammas);
    }

    let combos = build_combos(&counts);
    let n_combos = combos.len();

    let results: Vec<(f64, f64, f64, f64, f64)> = combos
        .par_iter()
        .map(|combo_indices| {
            let mut s = base_s.clone();
            let mut current_n = n_ports;

            for (p, &cand_idx) in combo_indices.iter().enumerate() {
                let gamma = &all_gammas[p][cand_idx];
                s = apply_termination(&s, current_n, nfreq, n_signals, gamma);
                current_n -= 1;
            }

            compute_metrics_n_with_targets(&s, nfreq, n_signals, &eval_ranges, &target_gamma)
        })
        .collect();

    let mut vswr_non_ant = Vec::with_capacity(n_combos);
    let mut vswr_ant = Vec::with_capacity(n_combos);
    let mut worst_il = Vec::with_capacity(n_combos);
    let mut target_non_ant = Vec::with_capacity(n_combos);
    let mut target_ant = Vec::with_capacity(n_combos);
    let mut combo_idx_flat: Vec<i32> = Vec::with_capacity(n_combos * n_tunable);

    for (i, (v_non_ant, v_ant, il, t_non_ant, t_ant)) in results.iter().enumerate() {
        vswr_non_ant.push(*v_non_ant);
        vswr_ant.push(*v_ant);
        worst_il.push(*il);
        target_non_ant.push(*t_non_ant);
        target_ant.push(*t_ant);
        for &ci in &combos[i] {
            combo_idx_flat.push(ci as i32);
        }
    }

    let vswr_s11_arr = PyArray1::from_vec_bound(py, vswr_non_ant);
    let vswr_s22_arr = PyArray1::from_vec_bound(py, vswr_ant);
    let worst_il_arr = PyArray1::from_vec_bound(py, worst_il);
    let target_s11_arr = PyArray1::from_vec_bound(py, target_non_ant);
    let target_s22_arr = PyArray1::from_vec_bound(py, target_ant);

    use numpy::ndarray::Array2;
    let arr2 = Array2::from_shape_vec(
        (n_combos, n_tunable),
        combo_idx_flat,
    ).unwrap();
    let combo_idx_arr = arr2.into_pyarray_bound(py);

    Ok((
        vswr_s11_arr,
        vswr_s22_arr,
        worst_il_arr,
        target_s11_arr,
        target_s22_arr,
        combo_idx_arr,
    ))
}

#[pymodule]
fn rf_sweep(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sweep_terminations_parallel, m)?)?;
    m.add_function(wrap_pyfunction!(sweep_terminations_parallel_targets, m)?)?;
    Ok(())
}
