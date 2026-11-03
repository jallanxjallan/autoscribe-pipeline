//! Book creation adapter for the existing exclusive-create Markdown writer.
use crate::{ServiceError, ServiceResult, operations::writenew};
use serde_json::{Value, json};
use std::{fs, io::BufRead, path::Path, process::Command};

fn invalid(message: impl Into<String>) -> ServiceError {
    ServiceError::InvalidInput(message.into())
}

fn identity(prefix: &str) -> ServiceResult<String> {
    use std::io::Read;
    let mut bytes = [0u8; 10];
    fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|e| ServiceError::Io(e.to_string()))?;
    let alphabet = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    let mut result = format!("{prefix}_");
    let (mut buffer, mut bits) = (0u32, 0);
    for byte in bytes {
        buffer = (buffer << 8) | byte as u32;
        bits += 8;
        while bits >= 5 {
            bits -= 5;
            result.push(alphabet[((buffer >> bits) & 31) as usize] as char);
        }
        buffer &= (1 << bits) - 1;
    }
    Ok(result)
}

pub fn run(input: impl BufRead, target: &Path, title: &str, dry_run: bool) -> ServiceResult<()> {
    if title.trim().is_empty() || title.chars().any(char::is_control) {
        return Err(invalid("book title must be nonempty single-line text"));
    }
    let target = fs::canonicalize(target).map_err(|e| ServiceError::Io(e.to_string()))?;
    // An explicit empty target makes retries fail closed, regardless of renamed chapters.
    if fs::read_dir(&target)
        .map_err(|e| ServiceError::Io(e.to_string()))?
        .next()
        .is_some()
    {
        return Err(invalid(
            "book destination must be empty; existing manuscripts are never overwritten",
        ));
    }
    let mut records = Vec::new();
    let mut source = String::new();
    for line in input.lines() {
        let line = line.map_err(|e| ServiceError::Io(e.to_string()))?;
        if line.trim().is_empty() {
            continue;
        }
        let record: Value = serde_json::from_str(&line).map_err(|e| invalid(e.to_string()))?;
        let metadata = record["input_record"]
            .as_object()
            .ok_or_else(|| invalid("missing input_record"))?;
        let id = metadata
            .get("source")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("missing source ID"))?;
        if id.is_empty()
            || !id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        {
            return Err(invalid("invalid Drive source ID"));
        }
        if metadata.len() != 2
            || record["input_record"]["chapter"].as_u64() != Some(records.len() as u64 + 1)
        {
            return Err(invalid(
                "book metadata must contain exactly source and consecutive chapter numbers starting at 1",
            ));
        }
        if !source.is_empty() && source != id {
            return Err(invalid("mixed source IDs"));
        }
        source = id.to_owned();
        let body = record["content"]
            .as_str()
            .ok_or_else(|| invalid("missing chapter content"))?;
        if body.trim().is_empty() || crate::markdown::split_frontmatter(body)?.0.is_some() {
            return Err(invalid(
                "chapter must have body content without frontmatter",
            ));
        }
        records.push(record);
    }
    if records.is_empty() {
        return Err(invalid("no chapters"));
    }
    let date = Command::new("/usr/bin/date")
        .arg("+%Y-%m-%d")
        .output()
        .map_err(|e| ServiceError::Io(e.to_string()))?;
    if !date.status.success() {
        return Err(invalid("cannot obtain creation date"));
    }
    let date = String::from_utf8_lossy(&date.stdout).trim().to_owned();
    let mut manifest = format!("# @title {title}\n\n");
    for (index, record) in records.iter_mut().enumerate() {
        let name = format!("Chapter {:04}", index + 1);
        let metadata = record["input_record"].as_object_mut().unwrap();
        metadata.extend(
            json!({"slug":identity("chp")?, "type":"chapter", "status":"draft",
            "created":date, "updated":date, "filename_hint":name})
            .as_object()
            .unwrap()
            .clone(),
        );
        manifest.push_str(&format!("[[{}]]\n\n", metadata["slug"].as_str().unwrap()));
    }
    records.push(
        json!({"input_record":{"slug":identity("pub")?, "type":"manifest",
        "status":"draft", "created":date, "updated":date, "pipeline":"typst", "output":"pdf",
        "filename_hint":"Book"}, "content":manifest}),
    );
    if dry_run {
        for record in &records {
            println!(
                "{}",
                json!({"path":target.join(format!("{}.md",record["input_record"]["filename_hint"].as_str().unwrap())), "record":record})
            );
        }
        return Ok(());
    }
    let stream = records.iter().map(|r| format!("{r}\n")).collect::<String>();
    writenew::run(std::io::Cursor::new(stream), Some(&target))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn canonical_book_and_retry_protection() {
        let target = std::env::temp_dir().join(identity("test").unwrap());
        fs::create_dir(&target).unwrap();
        let record = json!({"input_record":{"source":"drive_ID", "chapter":1},"content":"# Chapter 1\n\nOriginal *text*.\n"});
        let stream = format!("{record}\n");
        run(std::io::Cursor::new(&stream), &target, "Original", true).unwrap();
        assert_eq!(fs::read_dir(&target).unwrap().count(), 0);
        let invalid = format!(
            "{record}\n{}\n",
            json!({"input_record":{"source":"drive_ID","chapter":3},"content":"bad"})
        );
        assert!(run(std::io::Cursor::new(invalid), &target, "Original", false).is_err());
        assert_eq!(fs::read_dir(&target).unwrap().count(), 0);
        run(std::io::Cursor::new(&stream), &target, "Original", false).unwrap();
        let chapter = fs::read_to_string(target.join("Chapter 0001.md")).unwrap();
        assert!(chapter.contains("\"type\": \"chapter\""));
        assert!(chapter.contains("\"slug\": \"chp_"));
        assert!(chapter.ends_with("# Chapter 1\n\nOriginal *text*.\n"));
        assert!(
            fs::read_to_string(target.join("Book.md"))
                .unwrap()
                .contains("[[chp_")
        );
        fs::write(target.join("Chapter 0001.md"), "edited").unwrap();
        assert!(run(std::io::Cursor::new(&stream), &target, "Original", false).is_err());
        assert_eq!(
            fs::read_to_string(target.join("Chapter 0001.md")).unwrap(),
            "edited"
        );
        fs::remove_dir_all(target).unwrap();
    }
}
