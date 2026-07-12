//! Candidate parsing and strategy ranking for the BPM tuning tool.
//!
//! This crate deliberately has no third-party dependencies so the Python GUI can
//! build and invoke a small, portable CLI.

use std::cmp::Ordering;
use std::collections::HashSet;
use std::fmt;

const EXPECTED_COLUMNS: [&str; 6] = [
    "candidate_id",
    "bom_count",
    "max_vswr",
    "worst_il_db",
    "smith_radius",
    "target_distance",
];

const ACCEPTABLE_VSWR: f64 = 1.4;

/// A fully evaluated matching-network candidate.
#[derive(Debug, Clone, PartialEq)]
pub struct Candidate {
    pub id: String,
    pub bom_count: u32,
    pub max_vswr: f64,
    /// Positive insertion-loss magnitude in dB. Lower is better.
    pub worst_il_db: f64,
    pub smith_radius: f64,
    pub target_distance: f64,
}

/// Supported optimization personalities.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Strategy {
    MinimumBom,
    Balanced,
    LowestVswr,
    TightestContour,
    LowestInsertionLoss,
}

impl Strategy {
    pub const NAMES: [&'static str; 5] = [
        "minimum_bom",
        "balanced",
        "lowest_vswr",
        "tightest_contour",
        "lowest_insertion_loss",
    ];

    pub fn parse(value: &str) -> Result<Self, Error> {
        match value {
            "minimum_bom" => Ok(Self::MinimumBom),
            "balanced" => Ok(Self::Balanced),
            "lowest_vswr" => Ok(Self::LowestVswr),
            "tightest_contour" => Ok(Self::TightestContour),
            "lowest_insertion_loss" => Ok(Self::LowestInsertionLoss),
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

/// Parse an optional-header TSV document into validated candidates.
///
/// Blank lines are ignored. If a header is supplied it must exactly match the
/// documented six columns. Every metric must be finite and non-negative.
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

        let max_vswr = parse_metric(columns[2], "max_vswr", line_number)?;
        let worst_il_db = parse_metric(columns[3], "worst_il_db", line_number)?;
        let smith_radius = parse_metric(columns[4], "smith_radius", line_number)?;
        let target_distance = parse_metric(columns[5], "target_distance", line_number)?;

        candidates.push(Candidate {
            id: id.to_owned(),
            bom_count,
            max_vswr,
            worst_il_db,
            smith_radius,
            target_distance,
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
    if !parsed.is_finite() {
        return Err(Error::new(format!(
            "line {line_number}: {name} must be finite, got '{value}'"
        )));
    }
    if parsed < 0.0 {
        return Err(Error::new(format!(
            "line {line_number}: {name} must be non-negative, got '{value}'"
        )));
    }
    Ok(parsed)
}

/// Select the best candidate for `strategy`.
pub fn rank(strategy: Strategy, candidates: &[Candidate]) -> Result<&Candidate, Error> {
    if candidates.is_empty() {
        return Err(Error::new("cannot rank an empty candidate list"));
    }
    validate_candidates(candidates)?;

    match strategy {
        Strategy::MinimumBom => rank_minimum_bom(candidates),
        Strategy::Balanced => rank_balanced(candidates),
        Strategy::LowestVswr => Ok(min_by(candidates, compare_lowest_vswr)),
        Strategy::TightestContour => Ok(min_by(candidates, compare_tightest_contour)),
        Strategy::LowestInsertionLoss => Ok(min_by(candidates, compare_lowest_insertion_loss)),
    }
}

fn validate_candidates(candidates: &[Candidate]) -> Result<(), Error> {
    let mut ids = HashSet::new();
    for candidate in candidates {
        if candidate.id.trim().is_empty() {
            return Err(Error::new("candidate_id must not be empty"));
        }
        if !ids.insert(candidate.id.as_str()) {
            return Err(Error::new(format!(
                "duplicate candidate_id '{}'",
                candidate.id
            )));
        }
        for (name, value) in [
            ("max_vswr", candidate.max_vswr),
            ("worst_il_db", candidate.worst_il_db),
            ("smith_radius", candidate.smith_radius),
            ("target_distance", candidate.target_distance),
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

fn rank_minimum_bom(candidates: &[Candidate]) -> Result<&Candidate, Error> {
    let acceptable: Vec<&Candidate> = candidates
        .iter()
        .filter(|candidate| candidate.max_vswr <= ACCEPTABLE_VSWR)
        .collect();

    if let Some(best) = acceptable
        .into_iter()
        .min_by(|left, right| compare_minimum_bom(left, right))
    {
        return Ok(best);
    }

    // If the baseline is impossible, return the closest-to-acceptable match
    // instead of rewarding an electrically poor zero-component candidate.
    Ok(min_by(candidates, compare_lowest_vswr))
}

fn rank_balanced(candidates: &[Candidate]) -> Result<&Candidate, Error> {
    let ranges = Ranges::from_candidates(candidates);
    candidates
        .iter()
        .min_by(|left, right| {
            balanced_score(left, &ranges)
                .total_cmp(&balanced_score(right, &ranges))
                .then_with(|| compare_lowest_vswr(left, right))
        })
        .ok_or_else(|| Error::new("cannot rank an empty candidate list"))
}

fn min_by(candidates: &[Candidate], compare: fn(&Candidate, &Candidate) -> Ordering) -> &Candidate {
    candidates
        .iter()
        .min_by(|left, right| compare(left, right))
        .expect("caller validates non-empty candidates")
}

fn compare_minimum_bom(left: &Candidate, right: &Candidate) -> Ordering {
    left.bom_count
        .cmp(&right.bom_count)
        .then_with(|| left.max_vswr.total_cmp(&right.max_vswr))
        .then_with(|| left.worst_il_db.total_cmp(&right.worst_il_db))
        .then_with(|| left.smith_radius.total_cmp(&right.smith_radius))
        .then_with(|| left.target_distance.total_cmp(&right.target_distance))
        .then_with(|| left.id.cmp(&right.id))
}

fn compare_lowest_vswr(left: &Candidate, right: &Candidate) -> Ordering {
    left.max_vswr
        .total_cmp(&right.max_vswr)
        .then_with(|| left.bom_count.cmp(&right.bom_count))
        .then_with(|| left.worst_il_db.total_cmp(&right.worst_il_db))
        .then_with(|| left.smith_radius.total_cmp(&right.smith_radius))
        .then_with(|| left.target_distance.total_cmp(&right.target_distance))
        .then_with(|| left.id.cmp(&right.id))
}

fn compare_tightest_contour(left: &Candidate, right: &Candidate) -> Ordering {
    left.smith_radius
        .total_cmp(&right.smith_radius)
        .then_with(|| left.target_distance.total_cmp(&right.target_distance))
        .then_with(|| left.max_vswr.total_cmp(&right.max_vswr))
        .then_with(|| left.worst_il_db.total_cmp(&right.worst_il_db))
        .then_with(|| left.bom_count.cmp(&right.bom_count))
        .then_with(|| left.id.cmp(&right.id))
}

fn compare_lowest_insertion_loss(left: &Candidate, right: &Candidate) -> Ordering {
    left.worst_il_db
        .total_cmp(&right.worst_il_db)
        .then_with(|| left.max_vswr.total_cmp(&right.max_vswr))
        .then_with(|| left.bom_count.cmp(&right.bom_count))
        .then_with(|| left.smith_radius.total_cmp(&right.smith_radius))
        .then_with(|| left.target_distance.total_cmp(&right.target_distance))
        .then_with(|| left.id.cmp(&right.id))
}

#[derive(Debug)]
struct Ranges {
    bom: (f64, f64),
    vswr: (f64, f64),
    il: (f64, f64),
    smith: (f64, f64),
    target: (f64, f64),
}

impl Ranges {
    fn from_candidates(candidates: &[Candidate]) -> Self {
        let mut ranges = Self {
            bom: (f64::INFINITY, f64::NEG_INFINITY),
            vswr: (f64::INFINITY, f64::NEG_INFINITY),
            il: (f64::INFINITY, f64::NEG_INFINITY),
            smith: (f64::INFINITY, f64::NEG_INFINITY),
            target: (f64::INFINITY, f64::NEG_INFINITY),
        };
        for candidate in candidates {
            expand(&mut ranges.bom, f64::from(candidate.bom_count));
            expand(&mut ranges.vswr, candidate.max_vswr);
            expand(&mut ranges.il, candidate.worst_il_db);
            expand(&mut ranges.smith, candidate.smith_radius);
            expand(&mut ranges.target, candidate.target_distance);
        }
        ranges
    }
}

fn expand(range: &mut (f64, f64), value: f64) {
    range.0 = range.0.min(value);
    range.1 = range.1.max(value);
}

fn normalize(value: f64, range: (f64, f64)) -> f64 {
    let width = range.1 - range.0;
    if width <= f64::EPSILON {
        0.0
    } else {
        (value - range.0) / width
    }
}

fn balanced_score(candidate: &Candidate, ranges: &Ranges) -> f64 {
    // Match quality and loss intentionally dominate; the smaller weights keep
    // production cost and Smith-chart geometry relevant without overwhelming
    // the principal electrical objectives.
    0.40 * normalize(candidate.max_vswr, ranges.vswr)
        + 0.35 * normalize(candidate.worst_il_db, ranges.il)
        + 0.10 * normalize(candidate.smith_radius, ranges.smith)
        + 0.10 * normalize(candidate.target_distance, ranges.target)
        + 0.05 * normalize(f64::from(candidate.bom_count), ranges.bom)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candidate(
        id: &str,
        bom_count: u32,
        max_vswr: f64,
        worst_il_db: f64,
        smith_radius: f64,
        target_distance: f64,
    ) -> Candidate {
        Candidate {
            id: id.to_owned(),
            bom_count,
            max_vswr,
            worst_il_db,
            smith_radius,
            target_distance,
        }
    }

    #[test]
    fn parses_header_and_data() {
        let input = concat!(
            "candidate_id\tbom_count\tmax_vswr\tworst_il_db\tsmith_radius\ttarget_distance\n",
            "network-a\t2\t1.25\t0.35\t0.12\t0.08\n"
        );
        let parsed = parse_tsv(input).unwrap();
        assert_eq!(
            parsed,
            vec![candidate("network-a", 2, 1.25, 0.35, 0.12, 0.08)]
        );
    }

    #[test]
    fn parses_headerless_data_and_windows_line_endings() {
        let parsed = parse_tsv("a\t0\t1.1\t0.2\t0.3\t0.4\r\n").unwrap();
        assert_eq!(parsed[0].id, "a");
    }

    #[test]
    fn rejects_non_finite_and_duplicate_data() {
        assert!(parse_tsv("a\t0\tNaN\t0.2\t0.3\t0.4\n").is_err());
        assert!(parse_tsv("a\t0\t1.1\t0.2\t0.3\t0.4\na\t1\t1.2\t0.3\t0.4\t0.5\n").is_err());
    }

    #[test]
    fn minimum_bom_uses_acceptance_gate() {
        let candidates = vec![
            candidate("poor-no-parts", 0, 3.0, 0.0, 0.8, 0.8),
            candidate("two-parts", 2, 1.30, 0.4, 0.2, 0.2),
            candidate("three-parts", 3, 1.05, 0.2, 0.1, 0.1),
        ];
        assert_eq!(
            rank(Strategy::MinimumBom, &candidates).unwrap().id,
            "two-parts"
        );
    }

    #[test]
    fn minimum_bom_falls_back_to_best_vswr() {
        let candidates = vec![
            candidate("zero-parts", 0, 2.0, 0.1, 0.5, 0.5),
            candidate("closest", 3, 1.5, 0.5, 0.2, 0.2),
        ];
        assert_eq!(
            rank(Strategy::MinimumBom, &candidates).unwrap().id,
            "closest"
        );
    }

    #[test]
    fn direct_strategies_select_their_primary_metric() {
        let candidates = vec![
            candidate("best-vswr", 4, 1.05, 0.8, 0.3, 0.2),
            candidate("best-smith", 3, 1.2, 0.5, 0.05, 0.1),
            candidate("best-il", 2, 1.3, 0.1, 0.2, 0.3),
        ];
        assert_eq!(
            rank(Strategy::LowestVswr, &candidates).unwrap().id,
            "best-vswr"
        );
        assert_eq!(
            rank(Strategy::TightestContour, &candidates).unwrap().id,
            "best-smith"
        );
        assert_eq!(
            rank(Strategy::LowestInsertionLoss, &candidates).unwrap().id,
            "best-il"
        );
    }

    #[test]
    fn balanced_normalizes_different_metric_scales() {
        let candidates = vec![
            candidate("balanced", 2, 1.2, 0.3, 0.2, 0.2),
            candidate("low-vswr-high-loss", 5, 1.0, 3.0, 0.5, 0.5),
            candidate("low-loss-high-vswr", 1, 2.5, 0.0, 0.6, 0.6),
        ];
        assert_eq!(
            rank(Strategy::Balanced, &candidates).unwrap().id,
            "balanced"
        );
    }

    #[test]
    fn ties_are_deterministic_by_candidate_id() {
        let candidates = vec![
            candidate("z", 1, 1.2, 0.2, 0.1, 0.1),
            candidate("a", 1, 1.2, 0.2, 0.1, 0.1),
        ];
        assert_eq!(rank(Strategy::LowestVswr, &candidates).unwrap().id, "a");
    }
}
