use autoscribe_service::{ServiceError, ServiceResult, operations::writenew};
use std::{env, path::PathBuf, process::ExitCode};

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("writenew: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> ServiceResult<()> {
    let mut target_dir = None;
    let mut book_title = None;
    let mut dry_run = false;
    let args: Vec<_> = env::args().skip(1).collect();
    let mut index = 0usize;
    while index < args.len() {
        match args[index].as_str() {
            "--book-title" => {
                index += 1;
                book_title = Some(
                    args.get(index)
                        .ok_or_else(|| {
                            ServiceError::InvalidInput("--book-title requires text".into())
                        })?
                        .clone(),
                );
            }
            "--dry-run" => {
                dry_run = true;
            }
            "--target-dir" => {
                index += 1;
                target_dir = Some(PathBuf::from(args.get(index).ok_or_else(|| {
                    ServiceError::InvalidInput("--target-dir requires a path".into())
                })?));
            }
            value => {
                return Err(ServiceError::InvalidInput(format!(
                    "unknown writenew option: {value}"
                )));
            }
        }
        index += 1;
    }
    if let Some(title) = book_title {
        let target = target_dir
            .ok_or_else(|| ServiceError::InvalidInput("book mode requires --target-dir".into()))?;
        return autoscribe_service::operations::book::run(
            std::io::stdin().lock(),
            &target,
            &title,
            dry_run,
        );
    }
    if dry_run {
        return Err(ServiceError::InvalidInput(
            "--dry-run requires --book-title".into(),
        ));
    }
    writenew::run(std::io::stdin().lock(), target_dir.as_deref())
}
