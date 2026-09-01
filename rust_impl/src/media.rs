use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::UNIX_EPOCH;

use md5::{Digest, Md5};
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn io_error(error: std::io::Error) -> PyErr {
    PyOSError::new_err(error.to_string())
}

fn validate_relative_path(path: &str) -> PyResult<PathBuf> {
    let relative = Path::new(path);
    if relative.as_os_str().is_empty() || relative.is_absolute() {
        return Err(PyValueError::new_err(
            "media path must be a non-empty relative path",
        ));
    }
    if relative
        .components()
        .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(PyValueError::new_err(
            "media path cannot contain parent or current-directory components",
        ));
    }
    Ok(relative.to_path_buf())
}

fn checksum_reader(mut reader: impl Read) -> std::io::Result<String> {
    let mut digest = Md5::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

#[pyclass(module = "spideroxide._native")]
pub(crate) struct NativeMediaStore {
    root: PathBuf,
}

#[pymethods]
impl NativeMediaStore {
    #[new]
    fn new(root: PathBuf) -> PyResult<Self> {
        fs::create_dir_all(&root).map_err(io_error)?;
        Ok(Self { root })
    }

    fn persist(&self, relative_path: &str, content: &[u8]) -> PyResult<String> {
        let relative_path = validate_relative_path(relative_path)?;
        let target = self.root.join(relative_path);
        let parent = target
            .parent()
            .ok_or_else(|| PyValueError::new_err("media path has no parent directory"))?;
        fs::create_dir_all(parent).map_err(io_error)?;

        let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let filename = target
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("media");
        let temporary = parent.join(format!(
            ".{filename}.{}.{}.tmp",
            std::process::id(),
            sequence
        ));
        let result = (|| -> std::io::Result<()> {
            let mut file = OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&temporary)?;
            file.write_all(content)?;
            file.sync_all()?;
            fs::rename(&temporary, &target)
        })();
        if let Err(error) = result {
            let _ = fs::remove_file(&temporary);
            return Err(io_error(error));
        }

        let mut digest = Md5::new();
        digest.update(content);
        Ok(format!("{:x}", digest.finalize()))
    }

    fn stat(&self, relative_path: &str) -> PyResult<Option<(f64, String)>> {
        let relative_path = validate_relative_path(relative_path)?;
        let target = self.root.join(relative_path);
        let metadata = match fs::metadata(&target) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(io_error(error)),
        };
        let modified = metadata
            .modified()
            .map_err(io_error)?
            .duration_since(UNIX_EPOCH)
            .map_err(|error| PyOSError::new_err(error.to_string()))?
            .as_secs_f64();
        let checksum = checksum_reader(BufReader::new(File::open(target).map_err(io_error)?))
            .map_err(io_error)?;
        Ok(Some((modified, checksum)))
    }

    fn read<'py>(&self, py: Python<'py>, relative_path: &str) -> PyResult<Bound<'py, PyBytes>> {
        let relative_path = validate_relative_path(relative_path)?;
        let content = fs::read(self.root.join(relative_path)).map_err(io_error)?;
        Ok(PyBytes::new(py, &content))
    }
}
