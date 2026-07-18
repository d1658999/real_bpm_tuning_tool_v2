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
const SWEEP_INPUT_MAGIC: &[u8; 8] = b"BPMSWP02";
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
    bom_counts: Vec<Vec<u32>>,
    return_winners: bool,
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

#[derive(Debug, Clone)]
struct SweepCandidate {
    linear_index: usize,
    bom_count: u32,
    vswr_non_ant: f64,
    vswr_ant: f64,
    worst_il_db: f64,
    target_non_ant: f64,
    target_ant: f64,
    target_spread: f64,
    combination: Vec<usize>,
}

impl SweepCandidate {
    fn target_max(&self) -> f64 {
        self.target_non_ant.max(self.target_ant)
    }

    fn smith_score(&self) -> f64 {
        self.target_max() + self.target_spread
    }

    fn as_result(&self) -> SweepResult {
        SweepResult {
            vswr_non_ant: self.vswr_non_ant,
            vswr_ant: self.vswr_ant,
            worst_il_db: self.worst_il_db,
            target_non_ant: self.target_non_ant,
            target_ant: self.target_ant,
            combination: self.combination.clone(),
        }
    }
}

#[derive(Debug, Default)]
struct SweepAccumulator {
    results: Vec<SweepResult>,
    minimum_target: Option<SweepCandidate>,
    smith_contour: Option<SweepCandidate>,
    frontier: Vec<SweepCandidate>,
    bom_frontiers: Vec<Vec<SweepCandidate>>,
    target_max_range: (f64, f64),
    loss_range: (f64, f64),
    target_non_ant_floor: f64,
}

/// Run an exhaustive, parallel termination sweep using the bridge binary format.
pub fn sweep_file(input_path: &Path, output_path: &Path) -> Result<(), Error> {
    let bytes = fs::read(input_path)
        .map_err(|error| Error::new(format!("cannot read sweep input: {error}")))?;
    let problem = parse_sweep_problem(&bytes)?;
    let accumulator = sweep_problem(&problem)?;
    let results = if problem.return_winners {
        accumulator.winners()?
    } else {
        accumulator.results
    };
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
    let return_winners = match cursor.usize()? {
        0 => false,
        1 => true,
        _ => return Err(Error::new("invalid sweep output mode")),
    };
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
    let mut bom_counts = Vec::with_capacity(ntunable);
    for _ in 0..ntunable {
        let count = cursor.usize()?;
        if count == 0 {
            return Err(Error::new("each tunable port needs at least one candidate"));
        }
        let costs = (0..count)
            .map(|_| cursor.usize().map(|value| value as u32))
            .collect::<Result<Vec<_>, _>>()?;
        let flat = cursor.complex_vec(
            count
                .checked_mul(nfreq)
                .ok_or_else(|| Error::new("termination matrix is too large"))?,
        )?;
        bom_counts.push(costs);
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
        bom_counts,
        return_winners,
    })
}

impl SweepAccumulator {
    fn new(retain_all: bool, total: usize, ntunable: usize) -> Self {
        Self {
            results: if retain_all {
                Vec::with_capacity(total)
            } else {
                Vec::new()
            },
            minimum_target: None,
            smith_contour: None,
            frontier: Vec::new(),
            bom_frontiers: vec![Vec::new(); ntunable + 1],
            target_max_range: (f64::INFINITY, f64::NEG_INFINITY),
            loss_range: (f64::INFINITY, f64::NEG_INFINITY),
            target_non_ant_floor: f64::INFINITY,
        }
    }

    fn observe(
        &mut self,
        problem: &SweepProblem,
        linear_index: usize,
        combination: &[usize],
        metrics: (f64, f64, f64, f64, f64),
    ) {
        let (vswr_non_ant, vswr_ant, worst_il_db, target_non_ant, target_ant) = metrics;
        if problem.return_winners {
            let candidate = SweepCandidate {
                linear_index,
                bom_count: combination
                    .iter()
                    .enumerate()
                    .map(|(port, index)| problem.bom_counts[port][*index])
                    .sum(),
                vswr_non_ant,
                vswr_ant,
                worst_il_db,
                target_non_ant,
                target_ant,
                target_spread: (target_non_ant - target_ant).abs(),
                combination: combination.to_vec(),
            };
            self.target_max_range.0 = self.target_max_range.0.min(candidate.target_max());
            self.target_max_range.1 = self.target_max_range.1.max(candidate.target_max());
            self.loss_range.0 = self.loss_range.0.min(candidate.worst_il_db);
            self.loss_range.1 = self.loss_range.1.max(candidate.worst_il_db);
            self.target_non_ant_floor = self.target_non_ant_floor.min(candidate.target_non_ant);

            if self
                .minimum_target
                .as_ref()
                .is_none_or(|current| better_sweep_minimum_target(&candidate, current))
            {
                self.minimum_target = Some(candidate.clone());
            }
            if self
                .smith_contour
                .as_ref()
                .is_none_or(|current| better_sweep_smith_contour(&candidate, current))
            {
                self.smith_contour = Some(candidate.clone());
            }
            insert_pareto(&mut self.frontier, candidate.clone());
            let bom_count = candidate.bom_count as usize;
            insert_bom_pareto(&mut self.bom_frontiers[bom_count], candidate);
        } else {
            self.results.push(SweepResult {
                vswr_non_ant,
                vswr_ant,
                worst_il_db,
                target_non_ant,
                target_ant,
                combination: combination.to_vec(),
            });
        }
    }

    fn merge(&mut self, other: Self) {
        if !self.results.is_empty() || !other.results.is_empty() {
            self.results.extend(other.results);
            return;
        }

        self.target_max_range.0 = self.target_max_range.0.min(other.target_max_range.0);
        self.target_max_range.1 = self.target_max_range.1.max(other.target_max_range.1);
        self.loss_range.0 = self.loss_range.0.min(other.loss_range.0);
        self.loss_range.1 = self.loss_range.1.max(other.loss_range.1);
        self.target_non_ant_floor = self.target_non_ant_floor.min(other.target_non_ant_floor);

        if let Some(candidate) = other.minimum_target {
            if self
                .minimum_target
                .as_ref()
                .is_none_or(|current| better_sweep_minimum_target(&candidate, current))
            {
                self.minimum_target = Some(candidate);
            }
        }
        if let Some(candidate) = other.smith_contour {
            if self
                .smith_contour
                .as_ref()
                .is_none_or(|current| better_sweep_smith_contour(&candidate, current))
            {
                self.smith_contour = Some(candidate);
            }
        }
        for candidate in other.frontier {
            insert_pareto(&mut self.frontier, candidate);
        }
        for (index, candidates) in other.bom_frontiers.into_iter().enumerate() {
            for candidate in candidates {
                insert_bom_pareto(&mut self.bom_frontiers[index], candidate);
            }
        }
    }

    fn winners(self) -> Result<Vec<SweepResult>, Error> {
        let minimum_target = self
            .minimum_target
            .ok_or_else(|| Error::new("Rust sweep produced no candidates"))?;
        let smith_contour = self
            .smith_contour
            .ok_or_else(|| Error::new("Rust sweep produced no candidates"))?;

        let threshold = self.target_non_ant_floor * 1.10;
        let minimum_bom = self
            .bom_frontiers
            .iter()
            .flat_map(|candidates| candidates.iter())
            .filter(|candidate| candidate.target_non_ant <= threshold)
            .min_by(|left, right| compare_minimum_bom(left, right))
            .ok_or_else(|| Error::new("Rust sweep produced no minimum-BOM candidate"))?;

        let balanced = self
            .frontier
            .iter()
            .min_by(|left, right| {
                balanced_score_sweep(left, self.target_max_range, self.loss_range)
                    .total_cmp(&balanced_score_sweep(
                        right,
                        self.target_max_range,
                        self.loss_range,
                    ))
                    .then_with(|| compare_sweep_minimum_target(left, right))
            })
            .ok_or_else(|| Error::new("Rust sweep produced no balanced candidate"))?;

        let target_floor = self
            .frontier
            .iter()
            .map(SweepCandidate::target_max)
            .fold(f64::INFINITY, f64::min);
        let target_threshold = target_floor + 0.005_f64.max(0.15 * target_floor);
        let minimum_insertion_loss = self
            .frontier
            .iter()
            .filter(|candidate| candidate.target_max() <= target_threshold)
            .min_by(|left, right| compare_minimum_insertion_loss(left, right))
            .ok_or_else(|| Error::new("Rust sweep produced no minimum-loss candidate"))?;

        Ok(vec![
            minimum_bom.as_result(),
            balanced.as_result(),
            minimum_target.as_result(),
            smith_contour.as_result(),
            minimum_insertion_loss.as_result(),
        ])
    }
}

fn sweep_problem(problem: &SweepProblem) -> Result<SweepAccumulator, Error> {
    let counts: Vec<usize> = problem.gammas.iter().map(Vec::len).collect();
    let total = counts.iter().try_fold(1usize, |value, count| {
        value
            .checked_mul(*count)
            .ok_or_else(|| Error::new("too many BOM combinations"))
    })?;
    let workers = thread::available_parallelism()
        .map(usize::from)
        .unwrap_or(1)
        .min(if counts.is_empty() { 1 } else { counts[0] });
    let mut suffix_products = vec![1usize; counts.len() + 1];
    for index in (0..counts.len()).rev() {
        suffix_products[index] = suffix_products[index + 1]
            .checked_mul(counts[index])
            .ok_or_else(|| Error::new("too many BOM combinations"))?;
    }
    let mut aggregate = SweepAccumulator::new(problem.return_winners, total, counts.len());
    thread::scope(|scope| -> Result<(), Error> {
        let mut handles = Vec::new();
        for worker in 0..workers {
            let counts = &counts;
            let suffix_products = &suffix_products;
            handles.push(scope.spawn(move || {
                let mut local = SweepAccumulator::new(problem.return_winners, 0, counts.len());
                if counts.is_empty() {
                    visit_combinations(
                        problem,
                        counts,
                        suffix_products,
                        0,
                        &problem.base_s,
                        &mut Vec::new(),
                        0,
                        &mut local,
                    );
                    return local;
                }
                for first_candidate in (worker..counts[0]).step_by(workers) {
                    let first_s = apply_termination(
                        &problem.base_s,
                        problem.nports,
                        problem.nfreq,
                        problem.nsignals,
                        &problem.gammas[0][first_candidate],
                    );
                    let mut combination = vec![first_candidate];
                    visit_combinations(
                        problem,
                        counts,
                        suffix_products,
                        1,
                        &first_s,
                        &mut combination,
                        first_candidate * suffix_products[1],
                        &mut local,
                    );
                }
                local
            }));
        }
        for handle in handles {
            aggregate.merge(
                handle
                    .join()
                    .map_err(|_| Error::new("Rust sweep worker panicked"))?,
            );
        }
        Ok(())
    })?;
    if !problem.return_winners {
        aggregate.results.sort_by_key(|result| {
            // The worker traversal is lexicographic, but workers visit every
            // first-level branch in round-robin order. Recover reference order
            // for callers that intentionally request all results.
            result.combination.clone()
        });
    }
    Ok(aggregate)
}

fn visit_combinations(
    problem: &SweepProblem,
    counts: &[usize],
    suffix_products: &[usize],
    depth: usize,
    current_s: &[Complex],
    combination: &mut Vec<usize>,
    linear_index: usize,
    accumulator: &mut SweepAccumulator,
) {
    if depth == counts.len() {
        accumulator.observe(
            problem,
            linear_index,
            combination,
            compute_metrics(problem, current_s),
        );
        return;
    }

    let current_ports = problem.nports - depth;
    for candidate in 0..counts[depth] {
        let next_s = apply_termination(
            current_s,
            current_ports,
            problem.nfreq,
            problem.nsignals,
            &problem.gammas[depth][candidate],
        );
        combination.push(candidate);
        visit_combinations(
            problem,
            counts,
            suffix_products,
            depth + 1,
            &next_s,
            combination,
            linear_index + candidate * suffix_products[depth + 1],
            accumulator,
        );
        combination.pop();
    }
}

#[cfg(test)]
fn decode_combination(mut linear: usize, counts: &[usize]) -> Vec<usize> {
    let mut combination = vec![0usize; counts.len()];
    for index in (0..counts.len()).rev() {
        combination[index] = linear % counts[index];
        linear /= counts[index];
    }
    combination
}

fn compare_sweep_minimum_target(left: &SweepCandidate, right: &SweepCandidate) -> Ordering {
    left.target_non_ant
        .total_cmp(&right.target_non_ant)
        .then_with(|| left.target_ant.total_cmp(&right.target_ant))
        .then_with(|| left.bom_count.cmp(&right.bom_count))
        .then_with(|| left.worst_il_db.total_cmp(&right.worst_il_db))
        .then_with(|| left.linear_index.cmp(&right.linear_index))
}

fn better_sweep_minimum_target(left: &SweepCandidate, right: &SweepCandidate) -> bool {
    compare_sweep_minimum_target(left, right) == Ordering::Less
}

fn compare_minimum_bom(left: &SweepCandidate, right: &SweepCandidate) -> Ordering {
    left.bom_count
        .cmp(&right.bom_count)
        .then_with(|| {
            (left.target_non_ant + left.target_ant)
                .total_cmp(&(right.target_non_ant + right.target_ant))
        })
        .then_with(|| left.linear_index.cmp(&right.linear_index))
}

fn compare_sweep_smith_contour(left: &SweepCandidate, right: &SweepCandidate) -> Ordering {
    left.smith_score()
        .total_cmp(&right.smith_score())
        .then_with(|| left.target_max().total_cmp(&right.target_max()))
        .then_with(|| left.worst_il_db.total_cmp(&right.worst_il_db))
        .then_with(|| left.bom_count.cmp(&right.bom_count))
        .then_with(|| left.linear_index.cmp(&right.linear_index))
}

fn better_sweep_smith_contour(left: &SweepCandidate, right: &SweepCandidate) -> bool {
    compare_sweep_smith_contour(left, right) == Ordering::Less
}

fn compare_minimum_insertion_loss(left: &SweepCandidate, right: &SweepCandidate) -> Ordering {
    left.worst_il_db
        .total_cmp(&right.worst_il_db)
        .then_with(|| left.target_max().total_cmp(&right.target_max()))
        .then_with(|| left.linear_index.cmp(&right.linear_index))
}

fn balanced_score_sweep(
    candidate: &SweepCandidate,
    target_range: (f64, f64),
    loss_range: (f64, f64),
) -> f64 {
    normalize(candidate.target_max(), target_range) + normalize(candidate.worst_il_db, loss_range)
}

fn dominates(left: &SweepCandidate, right: &SweepCandidate) -> bool {
    let target_better_or_equal = left.target_max() <= right.target_max();
    let loss_better_or_equal = left.worst_il_db <= right.worst_il_db;
    let strict = left.target_max() < right.target_max() || left.worst_il_db < right.worst_il_db;
    target_better_or_equal && loss_better_or_equal && strict
}

fn insert_pareto(frontier: &mut Vec<SweepCandidate>, candidate: SweepCandidate) {
    if frontier
        .iter()
        .any(|existing| dominates(existing, &candidate))
    {
        return;
    }
    frontier.retain(|existing| !dominates(&candidate, existing));
    frontier.push(candidate);
}

fn bom_dominates(left: &SweepCandidate, right: &SweepCandidate) -> bool {
    let left_sum = left.target_non_ant + left.target_ant;
    let right_sum = right.target_non_ant + right.target_ant;
    let target_better_or_equal = left.target_non_ant <= right.target_non_ant;
    let sum_better_or_equal = left_sum <= right_sum;
    let strict = left.target_non_ant < right.target_non_ant || left_sum < right_sum;
    target_better_or_equal && sum_better_or_equal && strict
}

fn insert_bom_pareto(frontier: &mut Vec<SweepCandidate>, candidate: SweepCandidate) {
    if frontier
        .iter()
        .any(|existing| bom_dominates(existing, &candidate))
    {
        return;
    }
    frontier.retain(|existing| !bom_dominates(&candidate, existing));
    frontier.push(candidate);
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
        let inverse = Complex::new(1.0, 0.0).div(denominator);
        for i in 0..reduced_ports {
            let source_i = if i >= port_k { i + 1 } else { i };
            let factor = s[offset + source_i * nports + port_k]
                .mul(load)
                .mul(inverse);
            for j in 0..reduced_ports {
                let source_j = if j >= port_k { j + 1 } else { j };
                let s_ij = s[offset + source_i * nports + source_j];
                let update = factor.mul(s[offset + port_k * nports + source_j]);
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
            let sii_magnitude = sii.norm();
            invalid_passivity |= !sii_magnitude.is_finite() || sii_magnitude > PASSIVITY_LIMIT;
            vswr_non_ant = vswr_non_ant.max(vswr(sii_magnitude));
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
        let saa_magnitude = saa.norm();
        invalid_passivity |= !saa_magnitude.is_finite() || saa_magnitude > PASSIVITY_LIMIT;
        vswr_ant = vswr_ant.max(vswr(saa_magnitude));
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
