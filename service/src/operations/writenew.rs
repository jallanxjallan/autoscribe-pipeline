use crate::{ServiceError, ServiceResult, markdown};
use serde_json::Value;
use serde_yaml::{Mapping, Value as Yaml};
use std::{
    collections::HashSet,
    fs::{self, OpenOptions},
    io::{BufRead, Write},
    path::{Component, Path, PathBuf},
    process::Command,
};

fn invalid(message: impl Into<String>) -> ServiceError {
    ServiceError::InvalidInput(message.into())
}

fn io(error: std::io::Error) -> ServiceError {
    ServiceError::Io(error.to_string())
}

fn common(operation: &str) -> ServiceResult<String> {
    let result = Command::new("/usr/bin/python3")
        .args(["-B", "-m", "biznet_common.identity", operation, "passage"])
        .output()
        .map_err(io)?;
    if !result.status.success() {
        return Err(ServiceError::Process(format!(
            "Common identity: {}",
            String::from_utf8_lossy(&result.stderr).trim()
        )));
    }
    String::from_utf8(result.stdout)
        .map(|value| value.trim().to_owned())
        .map_err(|error| invalid(error.to_string()))
}

fn destination(vault: &Path) -> ServiceResult<PathBuf> {
    let config_path = vault.join("_tools/config.yaml");
    let config: Yaml = serde_yaml::from_str(
        &fs::read_to_string(&config_path)
            .map_err(|error| ServiceError::Io(format!("{}: {error}", config_path.display())))?,
    )
    .map_err(|error| invalid(format!("{}: {error}", config_path.display())))?;
    if config["schema_version"].as_u64() != Some(1) {
        return Err(invalid("Expected configuration schema version 1"));
    }
    let passage = &config["types"]["passage"];
    let identity: Value = serde_json::from_str(&common("contract")?)
        .map_err(|error| invalid(format!("Invalid Common identity contract: {error}")))?;
    if !passage["prefix"].is_null() && passage["prefix"].as_str() != identity["prefix"].as_str() {
        return Err(invalid("Configured passage prefix disagrees with Common"));
    }
    if config["slug"]["bits"].as_u64() != identity["bits"].as_u64()
        || config["slug"]["encoding"].as_str() != identity["encoding"].as_str()
        || config["slug"]["separator"].as_str() != identity["separator"].as_str()
    {
        return Err(invalid("Configured slug parameters disagree with Common"));
    }
    let route = passage["folder"]
        .as_str()
        .ok_or_else(|| invalid("Missing passage folder"))?;
    if route
        .split('/')
        .any(|part| part.is_empty() || matches!(part, "." | ".."))
    {
        return Err(invalid("Passage folder must be vault-relative"));
    }
    let mut folder = vault.to_owned();
    for part in Path::new(route).components() {
        let Component::Normal(part) = part else {
            return Err(invalid("Passage folder must be vault-relative"));
        };
        let name = part
            .to_str()
            .ok_or_else(|| invalid("Invalid passage folder"))?;
        if !name.starts_with(|ch: char| ch.is_ascii_lowercase())
            || name.split('_').any(|segment| {
                segment.is_empty()
                    || !segment
                        .bytes()
                        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
            })
        {
            return Err(invalid("Passage folder must use snake_case"));
        }
        folder.push(part);
        if !fs::symlink_metadata(&folder).map_err(io)?.is_dir() {
            return Err(invalid(format!(
                "Unsafe passage folder: {}",
                folder.display()
            )));
        }
    }
    if folder == vault {
        return Err(invalid("Passage folder cannot be the vault root"));
    }
    Ok(folder)
}

fn value<'a>(value: &'a Value, field: &str) -> ServiceResult<Option<&'a str>> {
    if value.is_null() {
        return Ok(None);
    }
    value
        .as_str()
        .filter(|value| !value.trim().is_empty())
        .map(Some)
        .ok_or_else(|| invalid(format!("{field} must be a non-empty string")))
}

fn slug_hint(slug: &str) -> String {
    if let Some(part) = slug
        .split('.')
        .nth(1)
        .filter(|part| !part.trim().is_empty())
    {
        let words: Vec<String> = part
            .split('-')
            .filter(|word| !word.is_empty())
            .map(|word| {
                let mut chars = word.chars();
                let first = chars.next().unwrap().to_uppercase().collect::<String>();
                first + &chars.as_str().to_lowercase()
            })
            .collect();
        if !words.is_empty() {
            return words.join(" ");
        }
    }
    slug.to_owned()
}

fn prepare(record: &Value) -> ServiceResult<(String, String, Option<String>)> {
    let input = record["input_record"]
        .as_object()
        .ok_or_else(|| invalid("Expected input_record object"))?;
    let source = value(&record["input_record"]["slug"], "input_record.slug")?;
    let fallback;
    let hint = match value(
        &record["input_record"]["filename_hint"],
        "input_record.filename_hint",
    )? {
        Some(hint) => hint,
        None => {
            fallback = slug_hint(
                source.ok_or_else(|| invalid("Expected a filename hint or source slug"))?,
            );
            &fallback
        }
    };
    let basename = Path::new(hint)
        .file_name()
        .and_then(|name| name.to_str())
        .map(str::trim)
        .filter(|name| {
            !name.is_empty()
                && !matches!(*name, "." | "..")
                && !name.contains(['\\', ':'])
                && !name.chars().any(char::is_control)
        })
        .ok_or_else(|| invalid("Invalid filename hint"))?;
    let filename = if basename.ends_with(".md") {
        basename.to_owned()
    } else {
        format!("{basename}.md")
    };
    if !record["destination_slug"].is_null()
        || !record["input_record"]["destination_slug"].is_null()
    {
        return Err(invalid("writenew assigns the destination slug"));
    }
    let content =
        value(&record["content"], "content")?.ok_or_else(|| invalid("Missing content"))?;
    let (envelope, body) = markdown::split_frontmatter(content)?;
    let newline = if content
        .split_inclusive('\n')
        .next()
        .is_some_and(|line| line.ends_with("\r\n"))
    {
        "\r\n"
    } else {
        "\n"
    };
    let mut raw = String::new();
    let mut metadata = Mapping::new();
    if let Some(envelope) = envelope {
        let start = envelope.find('\n').unwrap() + 1;
        let trimmed = envelope.trim_end_matches(['\r', '\n']);
        let header = &envelope[start..trimmed.len() - 3];
        metadata = serde_yaml::from_str::<Option<Mapping>>(header)
            .map_err(|error| invalid(format!("Invalid frontmatter: {error}")))?
            .unwrap_or_default();
        for key in ["type", "slug", "dispatch", "plan", "destination_slug"] {
            if metadata.contains_key(key) {
                if key == "destination_slug"
                    || (key == "slug" && (source.is_none() || metadata[key].as_str() != source))
                {
                    return Err(invalid("Contradictory destination identity in frontmatter"));
                }
            }
        }
        let mut retained = String::new();
        for line in header.split_inclusive('\n') {
            let key = line
                .split_once(':')
                .map(|(key, _)| key.trim_matches(['\'', '"']));
            if let Some("type" | "slug" | "dispatch" | "plan") = key {
                if !line.starts_with(char::is_whitespace) {
                    continue;
                }
            }
            retained.push_str(line);
        }
        let mut expected = metadata.clone();
        for key in ["type", "slug", "dispatch", "plan"] {
            expected.remove(key);
        }
        let actual = serde_yaml::from_str::<Option<Mapping>>(&retained)
            .map_err(|error| invalid(format!("Cannot safely edit workflow fields: {error}")))?
            .unwrap_or_default();
        if actual != expected {
            return Err(invalid("Workflow metadata must use standalone properties"));
        }
        raw = retained;
    }
    for (key, item) in input {
        if matches!(
            key.as_str(),
            "slug" | "type" | "dispatch" | "plan" | "destination_slug"
        ) || metadata.contains_key(key.as_str())
        {
            continue;
        }
        raw.push_str(&format!(
            "{}: {}{newline}",
            Value::String(key.clone()),
            item
        ));
    }
    if let Some(source) = source {
        // Keep the original provenance list verbatim and record the direct source separately.
        match metadata.get("source_slug").and_then(Yaml::as_str) {
            Some(existing) if existing == source => {}
            Some(_) => return Err(invalid("Contradictory source_slug in frontmatter")),
            None if metadata.contains_key("source_slug") => {
                return Err(invalid("Invalid source_slug in frontmatter"));
            }
            None => {
                if let Some(asserted) = input.get("source_slug") {
                    if asserted.as_str() != Some(source) {
                        return Err(invalid("Contradictory source_slug in input_record"));
                    }
                } else {
                    raw.push_str(&format!(
                        "source_slug: {}{newline}",
                        Value::String(source.to_owned())
                    ));
                }
            }
        }
    }
    let document = format!(
        "---{newline}type: passage{newline}dispatch: false{newline}plan: \"\"{newline}{raw}---{newline}{body}"
    );
    Ok((filename, document, source.map(str::to_owned)))
}

fn collect_slugs(directory: &Path, slugs: &mut HashSet<String>) -> ServiceResult<()> {
    for entry in fs::read_dir(directory).map_err(io)? {
        let entry = entry.map_err(io)?;
        let path = entry.path();
        let kind = entry.file_type().map_err(io)?;
        let name = entry.file_name();
        if kind.is_dir() && !name.to_string_lossy().starts_with('.') {
            collect_slugs(&path, slugs)?;
        } else if kind.is_file() && path.extension().is_some_and(|ext| ext == "md") {
            let text = fs::read_to_string(path).map_err(io)?;
            if let Ok((Some(header), _)) = markdown::split_frontmatter(&text) {
                if let Ok(metadata) = markdown::metadata(Some(header)) {
                    if let Some(slug) = metadata["slug"].as_str() {
                        slugs.insert(slug.to_owned());
                    }
                }
            }
        }
    }
    Ok(())
}

struct Files(Vec<PathBuf>);

impl Drop for Files {
    fn drop(&mut self) {
        for file in self.0.iter().rev() {
            let _ = fs::remove_file(file);
        }
    }
}

pub fn run(input: impl BufRead, target_dir: Option<&Path>) -> ServiceResult<()> {
    let vault = target_dir.unwrap_or_else(|| Path::new("."));
    let expanded;
    let vault = if let Ok(rest) = vault.strip_prefix("~") {
        expanded = PathBuf::from(std::env::var_os("HOME").ok_or_else(|| invalid("HOME is unset"))?)
            .join(rest);
        expanded.as_path()
    } else {
        vault
    };
    let vault = fs::canonicalize(vault).map_err(io)?;
    let folder = destination(&vault)?;
    // A directory lock serializes slug and filename checks across writers without a receipt file.
    let lock = fs::File::open(&folder).map_err(io)?;
    lock.lock().map_err(io)?;
    let mut records = Vec::new();
    let mut names = HashSet::new();
    for entry in fs::read_dir(&folder).map_err(io)? {
        names.insert(
            entry
                .map_err(io)?
                .file_name()
                .to_string_lossy()
                .to_lowercase(),
        );
    }
    for (index, line) in input.lines().enumerate() {
        let line = line.map_err(io)?;
        if line.trim().is_empty() {
            continue;
        }
        let record: Value = serde_json::from_str(&line)
            .map_err(|error| invalid(format!("NDJSON line {}: {error}", index + 1)))?;
        let prepared = prepare(&record)?;
        if !names.insert(prepared.0.to_lowercase()) {
            return Err(invalid(format!("Filename collision: {}", prepared.0)));
        }
        records.push(prepared);
    }
    if records.is_empty() {
        return Ok(());
    }
    let mut slugs = HashSet::new();
    collect_slugs(&vault, &mut slugs)?;
    slugs.extend(records.iter().filter_map(|record| record.2.clone()));
    let mut temporary = Files(Vec::new());
    let mut planned = Vec::new();
    for (name, document, _) in records {
        let mut reserved = None;
        for _ in 0..16 {
            let slug = common("generate")?;
            if !slugs.insert(slug.clone()) {
                continue;
            }
            let staging = folder.join(format!(".writenew-{slug}.tmp"));
            let mut file = match OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&staging)
            {
                Ok(file) => file,
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(io(error)),
            };
            temporary.0.push(staging.clone());
            let offset = document.find('\n').unwrap() + 1;
            let newline = if document[..offset].ends_with("\r\n") {
                "\r\n"
            } else {
                "\n"
            };
            file.write_all(document[..offset].as_bytes()).map_err(io)?;
            write!(file, "slug: {slug}{newline}").map_err(io)?;
            file.write_all(document[offset..].as_bytes()).map_err(io)?;
            file.sync_all().map_err(io)?;
            reserved = Some((staging, folder.join(name)));
            break;
        }
        planned.push(reserved.ok_or_else(|| invalid("Could not reserve a distinct passage slug"))?);
    }
    let mut published = Files(Vec::new());
    for (staging, destination) in planned {
        if fs::canonicalize(&folder).map_err(io)? != folder {
            return Err(invalid("Passage destination changed during write"));
        }
        fs::hard_link(staging, &destination).map_err(io)?;
        published.0.push(destination);
    }
    published.0.clear();
    Ok(())
}
