use std::env;
use std::fs;
use std::path::Path;
use std::process::ExitCode;

use bpm_ranking_optimizer::{Strategy, parse_tsv, rank, sweep_file};

fn main() -> ExitCode {
    match run(env::args()) {
        Ok(candidate_id) => {
            println!("{candidate_id}");
            ExitCode::SUCCESS
        }
        Err(message) => {
            eprintln!("error: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run<I>(args: I) -> Result<String, String>
where
    I: IntoIterator<Item = String>,
{
    let mut args = args.into_iter();
    let program = args
        .next()
        .unwrap_or_else(|| "bpm-ranking-optimizer".to_owned());
    let strategy_value = args.next().ok_or_else(|| usage(&program))?;
    let input_path = args.next().ok_or_else(|| usage(&program))?;
    if strategy_value == "sweep" {
        let output_path = args.next().ok_or_else(|| usage(&program))?;
        if args.next().is_some() {
            return Err(format!("too many arguments\n{}", usage(&program)));
        }
        sweep_file(Path::new(&input_path), Path::new(&output_path))
            .map_err(|error| error.to_string())?;
        return Ok(format!("{output_path}"));
    }
    if args.next().is_some() {
        return Err(format!("too many arguments\n{}", usage(&program)));
    }

    let strategy = Strategy::parse(&strategy_value).map_err(|error| error.to_string())?;
    let contents = fs::read_to_string(Path::new(&input_path))
        .map_err(|error| format!("cannot read input file '{input_path}': {error}"))?;
    let candidates = parse_tsv(&contents).map_err(|error| error.to_string())?;
    let best = rank(strategy, &candidates).map_err(|error| error.to_string())?;
    Ok(best.id.clone())
}

fn usage(program: &str) -> String {
    format!(
        "usage: {program} <strategy> <input.tsv>\n       {program} sweep <input.bin> <output.bin>\nstrategies: {}",
        Strategy::NAMES.join(", ")
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_arguments_returns_usage() {
        let error = run(["optimizer".to_owned()]).unwrap_err();
        assert!(error.contains("usage:"));
    }

    #[test]
    fn extra_arguments_are_rejected() {
        let error = run([
            "optimizer".to_owned(),
            "balanced".to_owned(),
            "input.tsv".to_owned(),
            "extra".to_owned(),
        ])
        .unwrap_err();
        assert!(error.contains("too many arguments"));
    }

    #[test]
    fn sweep_requires_an_output_path() {
        let error = run([
            "optimizer".to_owned(),
            "sweep".to_owned(),
            "input.bin".to_owned(),
        ])
        .unwrap_err();
        assert!(error.contains("usage:"));
    }
}
