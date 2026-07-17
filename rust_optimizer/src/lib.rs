//! Exhaustive RF termination sweep and five-agent ranking kernel.
//!
//! Python owns project configuration, Touchstone I/O, and the GUI. This crate
//! owns the expensive Cartesian termination sweep and deterministic selection
//! objectives adapted from `99_ reference/lib.rs` and
//! `99_ reference/fleet_optimizer.py`.

use std::cmp::Ordering;
use std::collections::HashSet;
use std::fmt;
use std::fs;
use std::path::Path;
use std::thread;

const EXPECTED_COLUMNS: [&str; 9] = [
    "candidate_id",
    "bom_count",
    "vswr_non_ant",
    "vswr_ant",
    "worst_il_db",
    "smith_score",
    "target_non_ant",
    "target_ant",
    "target_spread",
];
const SWEEP_INPUT_MAGIC: &[u8; 8] = b"BPMSWP01";
const SWEEP_OUTPUT_MAGIC: &[u8; 8] = b"BPMOUT01";
const PASSIVITY_LIMIT: f64 = 1.0 + 1e-9;
const INVALID_RF_PENALTY: f64 = 1e6;

/// Metrics for one fully evaluated BOM combination.
#[derive(Debug, Clone, PartialEq)]
pub struct Candidate {
    pub id: String,
    pub bom_count: u32,
    pub vswr_non_ant: f64,
    pub vswr_ant: f64,
    /// Positive insertion-loss magnitude in dB. Lower is better.
    pub worst_il_db: f64,
    /// Target-centred Smith contour score. Lower is better.
    pub smith_score: f64,
    pub target_non_ant: f64,
    pub target_ant: f64,
    pub target_spread: f64,
}

impl Candidate {
    pub fn target_max(&self) -> f64 {
        self.target_non_ant.max(self.target_ant)
    }
}

/// Five optimization personalities from the reference fleet optimizer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Strategy {
    MinimumBom,
    Balanced,
    MinimumTarget,
    SmithContour,
    MinimumInsertionLoss,
}

impl Strategy {
    pub const NAMES: [&'static str; 5] = [
        "minimum_bom",
        "balanced",
        "minimum_target",
        "smith_contour",
        "minimum_insertion_loss",
    ];

    pub fn parse(value: &str) -> Result<Self, Error> {
        match value {
            "minimum_bom" => Ok(Self::MinimumBom),
            "balanced" => Ok(Self::Balanced),
            "minimum_target" | "lowest_vswr" => Ok(Self::MinimumTarget),
            "smith_contour" | "tightest_contour" => Ok(Self::SmithContour),
            "minimum_insertion_loss" | "lowest_insertion_loss" => Ok(Self::MinimumInsertionLoss),
            _ => Err(Error::new(format!(
                "unknown strategy '{value}'; expected one of: {}",
                Self::NAMES.join(", ")
            ))),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    message: String,
}

impl Error {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for Error {}

/// Parse the bridge TSV into validated candidates.
pub fn parse_tsv(input: &str) -> Result<Vec<Candidate>, Error> {
    let mut candidates = Vec::new();
    let mut ids = HashSet::new();
    let mut saw_content = false;

    for (zero_based_line, raw_line) in input.lines().enumerate() {
        let line_number = zero_based_line + 1;
        let line = raw_line.trim_end_matches('\r');
        if line.trim().is_empty() {
            continue;
        }

        let columns: Vec<&str> = line.split('\t').collect();
        if !saw_content && columns.as_slice() == EXPECTED_COLUMNS {
            saw_content = true;
            continue;
        }
        saw_content = true;
        if columns.len() != EXPECTED_COLUMNS.len() {
            return Err(Error::new(format!(
                "line {line_number}: expected {} tab-separated columns ({}) but found {}",
                EXPECTED_COLUMNS.len(),
                EXPECTED_COLUMNS.join(", "),
                columns.len()
            )));
        }

        let id = columns[0].trim();
        if id.is_empty() {
            return Err(Error::new(format!(
                "line {line_number}: candidate_id must not be empty"
            )));
        }
        if !ids.insert(id.to_owned()) {
            return Err(Error::new(format!(
                "line {line_number}: duplicate candidate_id '{id}'"
            )));
        }
        let bom_count = columns[1].trim().parse::<u32>().map_err(|_| {
            Error::new(format!(
                "line {line_number}: invalid bom_count '{}'; expected a non-negative integer",
                columns[1].trim()
            ))
        })?;
        candidates.push(Candidate {
            id: id.to_owned(),
            bom_count,
            vswr_non_ant: parse_metric(columns[2], "vswr_non_ant", line_number)?,
            vswr_ant: parse_metric(columns[3], "vswr_ant", line_number)?,
            worst_il_db: parse_metric(columns[4], "worst_il_db", line_number)?,
            smith_score: parse_metric(columns[5], "smith_score", line_number)?,
            target_non_ant: parse_metric(columns[6], "target_non_ant", line_number)?,
            target_ant: parse_metric(columns[7], "target_ant", line_number)?,
            target_spread: parse_metric(columns[8], "target_spread", line_number)?,
        });
    }

    if candidates.is_empty() {
        return Err(Error::new("input contains no candidate rows"));
    }
    Ok(candidates)
}

fn parse_metric(value: &str, name: &str, line_number: usize) -> Result<f64, Error> {
    let value = value.trim();
    let parsed = value.parse::<f64>().map_err(|_| {
        Error::new(format!(
            "line {line_number}: invalid {name} '{value}'; expected a finite number"
        ))
    })?;
    if !parsed.is_finite() || parsed < 0.0 {
        return Err(Error::new(format!(
            "line {line_number}: {name} must be finite and non-negative, got '{value}'"
        )));
    }
    Ok(parsed)
}

/// Select the best candidate using the reference agent objective.
pub fn rank(strategy: Strategy, candidates: &[Candidate]) -> Result<&Candidate, Error> {
    if candidates.is_empty() {
        return Err(Error::new("cannot rank an empty candidate list"));
    }
    validate_candidates(candidates)?;
    match strategy {
        Strategy::MinimumBom => Ok(rank_minimum_bom(candidates)),
        Strategy::Balanced => Ok(rank_balanced(candidates)),
        Strategy::MinimumTarget => Ok(min_by(candidates, compare_minimum_target)),
        Strategy::SmithContour => Ok(min_by(candidates, compare_smith_contour)),
        Strategy::MinimumInsertionLoss => Ok(rank_minimum_insertion_loss(candidates)),
    }
}

fn validate_candidates(candidates: &[Candidate]) -> Result<(), Error> {
    let mut ids = HashSet::new();
    for candidate in candidates {
        if candidate.id.trim().is_empty() || !ids.insert(candidate.id.as_str()) {
            return Err(Error::new("candidate IDs must be non-empty and unique"));
        }
        for (name, value) in [
            ("vswr_non_ant", candidate.vswr_non_ant),
            ("vswr_ant", candidate.vswr_ant),
            ("worst_il_db", candidate.worst_il_db),
            ("smith_score", candidate.smith_score),
            ("target_non_ant", candidate.target_non_ant),
            ("target_ant", candidate.target_ant),
            ("target_spread", candidate.target_spread),
        ] {
            if !value.is_finite() || value < 0.0 {
                return Err(Error::new(format!(
                    "candidate '{}': {name} must be finite and non-negative",
                    candidate.id
                )));
            }
        }
    }
    Ok(())
}

fn rank_minimum_bom(candidates: &[Candidate]) -> &Candidate {
    let floor = candidates
        .iter()
        .map(|candidate| candidate.target_non_ant)
        .fold(f64::INFINITY, f64::min);
    let threshold = floor * 1.10;
    candidates
        .iter()
        .filter(|candidate| candidate.target_non_ant <= threshold)
        .min_by(|left, right| {
            left.bom_count
                .cmp(&right.bom_count)
                .then_with(|| {
                    (left.target_non_ant + left.target_ant)
                        .total_cmp(&(right.target_non_ant + right.target_ant))
                })
                .then_with(|| left.id.cmp(&right.id))
        })
        .expect("the target floor always leaves at least one candidate")
}

fn rank_balanced(candidates: &[Candidate]) -> &Candidate {
    let target_range = range(candidates.iter().map(Candidate::target_max));
    let loss_range = range(candidates.iter().map(|candidate| candidate.worst_il_db));
    candidates
        .iter()
        .min_by(|left, right| {
            balanced_score(left, target_range, loss_range)
                .total_cmp(&balanced_score(right, target_range, loss_range))
                .then_with(|| compare_minimum_target(left, right))
        })
        .expect("caller validates non-empty candidates")
}

fn rank_minimum_insertion_loss(candidates: &[Candidate]) -> &Candidate {
    let floor = candidates
        .iter()
        .map(Candidate::target_max)
        .fold(f64::INFINITY, f64::min);
    let threshold = floor + 0.005_f64.max(0.15 * floor);
    candidates
        .iter()
        .filter(|candidate| candidate.target_max() <= threshold)
        .min_by(|left, right| {
            left.worst_il_db
                .total_cmp(&right.worst_il_db)
                .then_with(|| left.target_max().total_cmp(&right.target_max()))
                .then_with(|| left.id.cmp(&right.id))
        })
        .expect("the target floor always leaves at least one candidate")
}

fn min_by(candidates: &[Candidate], compare: fn(&Candidate, &Candidate) -> Ordering) -> &Candidate {
    candidates
        .iter()
        .min_by(|left, right| compare(left, right))
        .expect("caller validates non-empty candidates")
}

fn compare_minimum_target(left: &Candidate, right: &Candidate) -> Ordering {
    left.target_non_ant
        .total_cmp(&right.target_non_ant)
        .then_with(|| left.target_ant.total_cmp(&right.target_ant))
        .then_with(|| left.bom_count.cmp(&right.bom_count))
        .then_with(|| left.worst_il_db.total_cmp(&right.worst_il_db))
        .then_with(|| left.id.cmp(&right.id))
}

fn compare_smith_contour(left: &Candidate, right: &Candidate) -> Ordering {
    left.smith_score
        .total_cmp(&right.smith_score)
        .then_with(|| left.target_max().total_cmp(&right.target_max()))
        .then_with(|| left.worst_il_db.total_cmp(&right.worst_il_db))
        .then_with(|| left.bom_count.cmp(&right.bom_count))
        .then_with(|| left.id.cmp(&right.id))
}

fn range(values: impl Iterator<Item = f64>) -> (f64, f64) {
    values.fold(
        (f64::INFINITY, f64::NEG_INFINITY),
        |(minimum, maximum), value| (minimum.min(value), maximum.max(value)),
    )
}

fn normalize(value: f64, range: (f64, f64)) -> f64 {
    let width = range.1 - range.0;
    if width <= f64::EPSILON {
        0.0
    } else {
        (value - range.0) / width
    }
}

fn balanced_score(candidate: &Candidate, target_range: (f64, f64), loss_range: (f64, f64)) -> f64 {
    normalize(candidate.target_max(), target_range) + normalize(candidate.worst_il_db, loss_range)
}

#[derive(Debug, Clone, Copy, Default)]
struct Complex {
    re: f64,
    im: f64,
}

impl Complex {
    fn new(re: f64, im: f64) -> Self {
        Self { re, im }
    }

    fn norm_sqr(self) -> f64 {
        self.re * self.re + self.im * self.im
    }

    fn norm(self) -> f64 {
        self.norm_sqr().sqrt()
    }

    fn add(self, other: Self) -> Self {
        Self::new(self.re + other.re, self.im + other.im)
    }

    fn sub(self, other: Self) -> Self {
        Self::new(self.re - other.re, self.im - other.im)
    }

    fn mul(self, other: Self) -> Self {
        Self::new(
            self.re * other.re - self.im * other.im,
            self.re * other.im + self.im * other.re,
        )
    }

    fn div(self, other: Self) -> Self {
        let denominator = other.norm_sqr();
        if denominator <= 1e-30 {
            return Self::new(1e15, 0.0);
        }
        Self::new(
            (self.re * other.re + self.im * other.im) / denominator,
            (self.im * other.re - self.re * other.im) / denominator,
        )
    }
}

#[derive(Debug)]
struct SweepProblem {
    nfreq: usize,
    nports: usize,
    nsignals: usize,
    eval_ranges: Vec<(usize, usize)>,
    base_s: Vec<Complex>,
    target_gamma: Vec<Complex>,
    gammas: Vec<Vec<Vec<Complex>>>,
}

#[derive(Debug, Clone)]
struct SweepResult {
    vswr_non_ant: f64,
    vswr_ant: f64,
    worst_il_db: f64,
    target_non_ant: f64,
    target_ant: f64,
    combination: Vec<usize>,
}

/// Run an exhaustive, parallel termination sweep using the bridge binary format.
pub fn sweep_file(input_path: &Path, output_path: &Path) -> Result<(), Error> {
    let bytes = fs::read(input_path)
        .map_err(|error| Error::new(format!("cannot read sweep input: {error}")))?;
    let problem = parse_sweep_problem(&bytes)?;
    let results = sweep_problem(&problem)?;
    write_sweep_results(output_path, &results, problem.gammas.len())
}

fn parse_sweep_problem(bytes: &[u8]) -> Result<SweepProblem, Error> {
    let mut cursor = Cursor::new(bytes);
    if cursor.take(8)? != SWEEP_INPUT_MAGIC {
        return Err(Error::new("invalid sweep input magic"));
    }
    let nfreq = cursor.usize()?;
    let nports = cursor.usize()?;
    let nsignals = cursor.usize()?;
    let ntunable = cursor.usize()?;
    if nfreq == 0 || nsignals < 2 || nports != nsignals + ntunable {
        return Err(Error::new("invalid sweep dimensions"));
    }
    let mut eval_ranges = Vec::with_capacity(nsignals);
    for _ in 0..nsignals {
        let start = cursor.usize()?;
        let stop = cursor.usize()?;
        if start > stop || stop >= nfreq {
            return Err(Error::new("invalid sweep evaluation range"));
        }
        eval_ranges.push((start, stop));
    }
    let base_count = nfreq
        .checked_mul(nports)
        .and_then(|value| value.checked_mul(nports))
        .ok_or_else(|| Error::new("sweep base matrix is too large"))?;
    let base_s = cursor.complex_vec(base_count)?;
    let target_gamma = cursor.complex_vec(
        nfreq
            .checked_mul(nsignals)
            .ok_or_else(|| Error::new("target matrix is too large"))?,
    )?;
    let mut gammas = Vec::with_capacity(ntunable);
    for _ in 0..ntunable {
        let count = cursor.usize()?;
        if count == 0 {
            return Err(Error::new("each tunable port needs at least one candidate"));
        }
        let flat = cursor.complex_vec(
            count
                .checked_mul(nfreq)
                .ok_or_else(|| Error::new("termination matrix is too large"))?,
        )?;
        gammas.push(flat.chunks_exact(nfreq).map(|row| row.to_vec()).collect());
    }
    if cursor.remaining() != 0 {
        return Err(Error::new("unexpected trailing bytes in sweep input"));
    }
    Ok(SweepProblem {
        nfreq,
        nports,
        nsignals,
        eval_ranges,
        base_s,
        target_gamma,
        gammas,
    })
}

fn sweep_problem(problem: &SweepProblem) -> Result<Vec<SweepResult>, Error> {
    let counts: Vec<usize> = problem.gammas.iter().map(Vec::len).collect();
    let total = counts.iter().try_fold(1usize, |value, count| {
        value
            .checked_mul(*count)
            .ok_or_else(|| Error::new("too many BOM combinations"))
    })?;
    let workers = thread::available_parallelism()
        .map(usize::from)
        .unwrap_or(1)
        .min(total.max(1));
    let chunk = total.div_ceil(workers);
    let mut results = Vec::with_capacity(total);
    thread::scope(|scope| -> Result<(), Error> {
        let mut handles = Vec::new();
        for worker in 0..workers {
            let start = worker * chunk;
            let stop = (start + chunk).min(total);
            if start >= stop {
                continue;
            }
            let counts = &counts;
            handles.push(scope.spawn(move || {
                (start..stop)
                    .map(|linear| evaluate_combination(problem, decode_combination(linear, counts)))
                    .collect::<Vec<_>>()
            }));
        }
        for handle in handles {
            results.extend(
                handle
                    .join()
                    .map_err(|_| Error::new("Rust sweep worker panicked"))?,
            );
        }
        Ok(())
    })?;
    Ok(results)
}

fn decode_combination(mut linear: usize, counts: &[usize]) -> Vec<usize> {
    let mut combination = vec![0usize; counts.len()];
    for index in (0..counts.len()).rev() {
        combination[index] = linear % counts[index];
        linear /= counts[index];
    }
    combination
}

fn evaluate_combination(problem: &SweepProblem, combination: Vec<usize>) -> SweepResult {
    let mut s = problem.base_s.clone();
    let mut current_ports = problem.nports;
    for (port, candidate) in combination.iter().copied().enumerate() {
        s = apply_termination(
            &s,
            current_ports,
            problem.nfreq,
            problem.nsignals,
            &problem.gammas[port][candidate],
        );
        current_ports -= 1;
    }
    let (vswr_non_ant, vswr_ant, worst_il_db, target_non_ant, target_ant) =
        compute_metrics(problem, &s);
    SweepResult {
        vswr_non_ant,
        vswr_ant,
        worst_il_db,
        target_non_ant,
        target_ant,
        combination,
    }
}

fn apply_termination(
    s: &[Complex],
    nports: usize,
    nfreq: usize,
    port_k: usize,
    gamma: &[Complex],
) -> Vec<Complex> {
    let reduced_ports = nports - 1;
    let mut result = vec![Complex::default(); nfreq * reduced_ports * reduced_ports];
    for frequency in 0..nfreq {
        let offset = frequency * nports * nports;
        let s_kk = s[offset + port_k * nports + port_k];
        let load = gamma[frequency];
        let denominator = Complex::new(1.0, 0.0).sub(s_kk.mul(load));
        for i in 0..reduced_ports {
            let source_i = if i >= port_k { i + 1 } else { i };
            for j in 0..reduced_ports {
                let source_j = if j >= port_k { j + 1 } else { j };
                let s_ij = s[offset + source_i * nports + source_j];
                let update = s[offset + source_i * nports + port_k]
                    .mul(load)
                    .mul(s[offset + port_k * nports + source_j])
                    .div(denominator);
                result[frequency * reduced_ports * reduced_ports + i * reduced_ports + j] =
                    s_ij.add(update);
            }
        }
    }
    result
}

fn compute_metrics(problem: &SweepProblem, s: &[Complex]) -> (f64, f64, f64, f64, f64) {
    let n = problem.nsignals;
    let antenna = n - 1;
    let mut vswr_non_ant = 1.0_f64;
    let mut vswr_ant = 1.0_f64;
    let mut worst_il_db = 0.0_f64;
    let mut target_non_ant = 0.0_f64;
    let mut target_ant = 0.0_f64;
    let mut invalid_passivity = false;

    for port in 0..antenna {
        let (start, stop) = problem.eval_ranges[port];
        for frequency in start..=stop {
            let sii = s[frequency * n * n + port * n + port];
            invalid_passivity |= !sii.norm().is_finite() || sii.norm() > PASSIVITY_LIMIT;
            vswr_non_ant = vswr_non_ant.max(vswr(sii.norm()));
            let target = problem.target_gamma[port * problem.nfreq + frequency];
            target_non_ant = target_non_ant.max(finite_penalty(sii.sub(target).norm()));
            let raw_transmission = s[frequency * n * n + antenna * n + port].norm();
            invalid_passivity |=
                !raw_transmission.is_finite() || raw_transmission > PASSIVITY_LIMIT;
            let transmission = raw_transmission.max(1e-15);
            worst_il_db = worst_il_db.max((-20.0 * transmission.log10()).max(0.0));
        }
    }
    let (start, stop) = problem.eval_ranges[antenna];
    for frequency in start..=stop {
        let saa = s[frequency * n * n + antenna * n + antenna];
        invalid_passivity |= !saa.norm().is_finite() || saa.norm() > PASSIVITY_LIMIT;
        vswr_ant = vswr_ant.max(vswr(saa.norm()));
        let target = problem.target_gamma[antenna * problem.nfreq + frequency];
        target_ant = target_ant.max(finite_penalty(saa.sub(target).norm()));
    }
    if invalid_passivity {
        // All five agents must reject a numerically active/non-passive result.
        // Merely clipping VSWR is insufficient when a near-edge Smith target
        // makes |Gamma| > 1 look deceptively close to the requested target.
        (
            INVALID_RF_PENALTY,
            INVALID_RF_PENALTY,
            INVALID_RF_PENALTY,
            INVALID_RF_PENALTY,
            INVALID_RF_PENALTY,
        )
    } else {
        (
            finite_penalty(vswr_non_ant),
            finite_penalty(vswr_ant),
            finite_penalty(worst_il_db),
            target_non_ant,
            target_ant,
        )
    }
}

fn vswr(magnitude: f64) -> f64 {
    let clipped = finite_penalty(magnitude).min(0.99999);
    (1.0 + clipped) / (1.0 - clipped)
}

fn finite_penalty(value: f64) -> f64 {
    if value.is_finite() {
        value.clamp(0.0, INVALID_RF_PENALTY)
    } else {
        INVALID_RF_PENALTY
    }
}

fn write_sweep_results(path: &Path, results: &[SweepResult], ntunable: usize) -> Result<(), Error> {
    let row_bytes = 5usize
        .checked_mul(8)
        .and_then(|value| value.checked_add(ntunable.checked_mul(8)?))
        .ok_or_else(|| Error::new("sweep output is too large"))?;
    let mut output = Vec::with_capacity(24 + results.len() * row_bytes);
    output.extend_from_slice(SWEEP_OUTPUT_MAGIC);
    output.extend_from_slice(&(results.len() as u64).to_le_bytes());
    output.extend_from_slice(&(ntunable as u64).to_le_bytes());
    for result in results {
        for value in [
            result.vswr_non_ant,
            result.vswr_ant,
            result.worst_il_db,
            result.target_non_ant,
            result.target_ant,
        ] {
            output.extend_from_slice(&value.to_le_bytes());
        }
        for index in &result.combination {
            output.extend_from_slice(&(*index as u64).to_le_bytes());
        }
    }
    fs::write(path, output)
        .map_err(|error| Error::new(format!("cannot write sweep output: {error}")))
}

struct Cursor<'a> {
    bytes: &'a [u8],
    position: usize,
}

impl<'a> Cursor<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, position: 0 }
    }

    fn take(&mut self, count: usize) -> Result<&'a [u8], Error> {
        let stop = self
            .position
            .checked_add(count)
            .ok_or_else(|| Error::new("invalid sweep input length"))?;
        let value = self
            .bytes
            .get(self.position..stop)
            .ok_or_else(|| Error::new("truncated sweep input"))?;
        self.position = stop;
        Ok(value)
    }

    fn usize(&mut self) -> Result<usize, Error> {
        let raw: [u8; 8] = self.take(8)?.try_into().expect("eight bytes requested");
        usize::try_from(u64::from_le_bytes(raw))
            .map_err(|_| Error::new("sweep dimension does not fit this platform"))
    }

    fn f64(&mut self) -> Result<f64, Error> {
        let raw: [u8; 8] = self.take(8)?.try_into().expect("eight bytes requested");
        Ok(f64::from_le_bytes(raw))
    }

    fn complex_vec(&mut self, count: usize) -> Result<Vec<Complex>, Error> {
        (0..count)
            .map(|_| Ok(Complex::new(self.f64()?, self.f64()?)))
            .collect()
    }

    fn remaining(&self) -> usize {
        self.bytes.len() - self.position
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candidate(
        id: &str,
        bom_count: u32,
        loss: f64,
        smith: f64,
        target_non_ant: f64,
        target_ant: f64,
    ) -> Candidate {
        Candidate {
            id: id.to_owned(),
            bom_count,
            vswr_non_ant: 1.2,
            vswr_ant: 1.3,
            worst_il_db: loss,
            smith_score: smith,
            target_non_ant,
            target_ant,
            target_spread: (target_non_ant - target_ant).abs(),
        }
    }

    #[test]
    fn parses_target_aware_candidate_rows() {
        let input = concat!(
            "candidate_id\tbom_count\tvswr_non_ant\tvswr_ant\tworst_il_db\tsmith_score\ttarget_non_ant\ttarget_ant\ttarget_spread\n",
            "network-a\t2\t1.25\t1.30\t0.35\t0.12\t0.08\t0.09\t0.01\n"
        );
        let parsed = parse_tsv(input).unwrap();
        assert_eq!(parsed[0].id, "network-a");
        assert_eq!(parsed[0].target_max(), 0.09);
    }

    #[test]
    fn minimum_bom_uses_ten_percent_target_floor() {
        let candidates = vec![
            candidate("poor-open", 0, 0.0, 0.8, 0.30, 0.30),
            candidate("near-two", 2, 0.4, 0.2, 0.105, 0.12),
            candidate("best-three", 3, 0.2, 0.1, 0.10, 0.10),
        ];
        assert_eq!(
            rank(Strategy::MinimumBom, &candidates).unwrap().id,
            "near-two"
        );
    }

    #[test]
    fn reference_agent_objectives_select_expected_candidates() {
        let candidates = vec![
            candidate("best-target", 4, 0.8, 0.3, 0.05, 0.06),
            candidate("best-smith", 3, 0.5, 0.01, 0.08, 0.08),
            candidate("best-il", 2, 0.1, 0.2, 0.055, 0.06),
        ];
        assert_eq!(
            rank(Strategy::MinimumTarget, &candidates).unwrap().id,
            "best-target"
        );
        assert_eq!(
            rank(Strategy::SmithContour, &candidates).unwrap().id,
            "best-smith"
        );
        assert_eq!(
            rank(Strategy::MinimumInsertionLoss, &candidates)
                .unwrap()
                .id,
            "best-il"
        );
    }

    #[test]
    fn balanced_normalizes_target_error_and_loss() {
        let candidates = vec![
            candidate("balanced", 2, 0.3, 0.2, 0.2, 0.2),
            candidate("low-target-high-loss", 5, 3.0, 0.5, 0.0, 0.0),
            candidate("low-loss-high-target", 1, 0.0, 0.6, 1.0, 1.0),
        ];
        assert_eq!(
            rank(Strategy::Balanced, &candidates).unwrap().id,
            "balanced"
        );
    }

    #[test]
    fn combination_decoder_matches_reference_order() {
        let counts = [2, 3];
        assert_eq!(decode_combination(0, &counts), vec![0, 0]);
        assert_eq!(decode_combination(1, &counts), vec![0, 1]);
        assert_eq!(decode_combination(5, &counts), vec![1, 2]);
    }
}
