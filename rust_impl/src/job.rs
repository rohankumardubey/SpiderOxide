use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::ErrorKind;
use std::path::Path;
use std::time::Duration;

use fs2::FileExt;
use pyo3::exceptions::PyRuntimeError;
use pyo3::{PyErr, PyResult};
use rusqlite::{Connection, OptionalExtension, Transaction, params};

const SCHEMA_VERSION: i64 = 2;

pub(crate) struct PersistedRequest {
    pub(crate) request_id: u64,
    pub(crate) sequence: u64,
    pub(crate) priority: String,
    pub(crate) is_start_request: bool,
    pub(crate) payload: Vec<u8>,
}

pub(crate) struct PersistentJobStore {
    connection: Connection,
    lock_file: File,
}

fn job_error(context: &str, error: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(format!("{context}: {error}"))
}

fn sqlite_integer(value: u64, name: &str) -> PyResult<i64> {
    i64::try_from(value)
        .map_err(|_| PyRuntimeError::new_err(format!("{name} exceeds the persistent store limit")))
}

fn unsigned_integer(value: i64, name: &str) -> PyResult<u64> {
    u64::try_from(value).map_err(|_| {
        PyRuntimeError::new_err(format!(
            "persistent job store contains a negative {name}: {value}"
        ))
    })
}

impl PersistentJobStore {
    pub(crate) fn open(path: impl AsRef<Path>) -> PyResult<Self> {
        let directory = path.as_ref().to_path_buf();
        fs::create_dir_all(&directory)
            .map_err(|error| job_error("unable to create JOBDIR", error))?;

        let lock_path = directory.join(".spideroxide.lock");
        let lock_file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(&lock_path)
            .map_err(|error| job_error("unable to open JOBDIR lock", error))?;
        if let Err(error) = lock_file.try_lock_exclusive() {
            let message = if error.kind() == ErrorKind::WouldBlock {
                format!("JOBDIR is already in use: {}", directory.display())
            } else {
                format!("unable to lock JOBDIR {}: {error}", directory.display())
            };
            return Err(PyRuntimeError::new_err(message));
        }

        let database_path = directory.join("job.sqlite3");
        let mut connection = Connection::open(&database_path)
            .map_err(|error| job_error("unable to open persistent job database", error))?;
        connection
            .busy_timeout(Duration::from_secs(5))
            .map_err(|error| job_error("unable to configure persistent job database", error))?;
        connection
            .execute_batch(
                "
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fingerprints (
                    fingerprint BLOB PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS requests (
                    request_id INTEGER PRIMARY KEY,
                    sequence INTEGER NOT NULL UNIQUE,
                    priority TEXT NOT NULL,
                    is_start INTEGER NOT NULL CHECK (is_start IN (0, 1)),
                    payload BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_state (
                    key TEXT PRIMARY KEY,
                    payload BLOB NOT NULL
                );
                INSERT OR IGNORE INTO metadata(key, value)
                VALUES ('schema_version', 2);
                ",
            )
            .map_err(|error| job_error("unable to initialize persistent job database", error))?;

        let version: i64 = connection
            .query_row(
                "SELECT value FROM metadata WHERE key = 'schema_version'",
                [],
                |row| row.get(0),
            )
            .map_err(|error| job_error("unable to read persistent job schema", error))?;
        if version == 1 {
            let transaction = connection
                .transaction()
                .map_err(|error| job_error("unable to start persistent job migration", error))?;
            transaction
                .execute_batch(
                    "
                    ALTER TABLE requests
                    ADD COLUMN is_start INTEGER NOT NULL DEFAULT 0
                    CHECK (is_start IN (0, 1));
                    UPDATE metadata SET value = 2 WHERE key = 'schema_version';
                    ",
                )
                .map_err(|error| job_error("unable to migrate persistent job schema", error))?;
            transaction
                .commit()
                .map_err(|error| job_error("unable to commit persistent job migration", error))?;
        } else if version != SCHEMA_VERSION {
            return Err(PyRuntimeError::new_err(format!(
                "unsupported JOBDIR schema version {version}; expected {SCHEMA_VERSION}"
            )));
        }

        Ok(Self {
            connection,
            lock_file,
        })
    }

    pub(crate) fn load_requests(&self) -> PyResult<Vec<PersistedRequest>> {
        let mut statement = self
            .connection
            .prepare(
                "
                SELECT request_id, sequence, priority, is_start, payload
                FROM requests
                ORDER BY sequence ASC
                ",
            )
            .map_err(|error| job_error("unable to prepare persisted request recovery", error))?;
        let rows = statement
            .query_map([], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, bool>(3)?,
                    row.get::<_, Vec<u8>>(4)?,
                ))
            })
            .map_err(|error| job_error("unable to read persisted requests", error))?;

        rows.map(|row| {
            let (request_id, sequence, priority, is_start_request, payload) =
                row.map_err(|error| job_error("unable to decode persisted request", error))?;
            Ok(PersistedRequest {
                request_id: unsigned_integer(request_id, "request identifier")?,
                sequence: unsigned_integer(sequence, "request sequence")?,
                priority,
                is_start_request,
                payload,
            })
        })
        .collect()
    }

    pub(crate) fn load_fingerprints(&self) -> PyResult<HashSet<[u8; 32]>> {
        let mut statement = self
            .connection
            .prepare("SELECT fingerprint FROM fingerprints")
            .map_err(|error| job_error("unable to prepare fingerprint recovery", error))?;
        let rows = statement
            .query_map([], |row| row.get::<_, Vec<u8>>(0))
            .map_err(|error| job_error("unable to read persisted fingerprints", error))?;
        let mut fingerprints = HashSet::new();
        for row in rows {
            let value =
                row.map_err(|error| job_error("unable to decode persisted fingerprint", error))?;
            let fingerprint: [u8; 32] = value.try_into().map_err(|value: Vec<u8>| {
                PyRuntimeError::new_err(format!(
                    "persistent job store contains a {} byte fingerprint; expected 32",
                    value.len()
                ))
            })?;
            fingerprints.insert(fingerprint);
        }
        Ok(fingerprints)
    }

    fn insert_fingerprint(transaction: &Transaction<'_>, fingerprint: &[u8; 32]) -> PyResult<bool> {
        let inserted = transaction
            .execute(
                "INSERT OR IGNORE INTO fingerprints(fingerprint) VALUES (?1)",
                params![fingerprint.as_slice()],
            )
            .map_err(|error| job_error("unable to persist request fingerprint", error))?;
        Ok(inserted == 1)
    }

    pub(crate) fn schedule(
        &mut self,
        request_id: u64,
        sequence: u64,
        priority: &str,
        payload: Option<&[u8]>,
        fingerprint: Option<&[u8; 32]>,
        is_start_request: bool,
    ) -> PyResult<bool> {
        let transaction = self
            .connection
            .transaction()
            .map_err(|error| job_error("unable to start persistent schedule transaction", error))?;
        if let Some(fingerprint) = fingerprint
            && !Self::insert_fingerprint(&transaction, fingerprint)?
        {
            return Ok(false);
        }
        if let Some(payload) = payload {
            transaction
                .execute(
                    "
                    INSERT INTO requests(request_id, sequence, priority, is_start, payload)
                    VALUES (?1, ?2, ?3, ?4, ?5)
                    ",
                    params![
                        sqlite_integer(request_id, "request identifier")?,
                        sqlite_integer(sequence, "request sequence")?,
                        priority,
                        is_start_request,
                        payload,
                    ],
                )
                .map_err(|error| job_error("unable to persist scheduled request", error))?;
        }
        transaction
            .commit()
            .map_err(|error| job_error("unable to commit scheduled request", error))?;
        Ok(true)
    }

    pub(crate) fn complete(&mut self, request_id: u64) -> PyResult<()> {
        self.connection
            .execute(
                "DELETE FROM requests WHERE request_id = ?1",
                params![sqlite_integer(request_id, "request identifier")?],
            )
            .map_err(|error| job_error("unable to remove completed persisted request", error))?;
        Ok(())
    }

    pub(crate) fn load_spider_state(&self) -> PyResult<Option<Vec<u8>>> {
        self.connection
            .query_row(
                "SELECT payload FROM job_state WHERE key = 'spider'",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(|error| job_error("unable to load persisted spider state", error))
    }

    pub(crate) fn save_spider_state(&mut self, payload: &[u8]) -> PyResult<()> {
        self.connection
            .execute(
                "
                INSERT INTO job_state(key, payload) VALUES ('spider', ?1)
                ON CONFLICT(key) DO UPDATE SET payload = excluded.payload
                ",
                params![payload],
            )
            .map_err(|error| job_error("unable to persist spider state", error))?;
        Ok(())
    }

    pub(crate) fn close(self) -> PyResult<()> {
        let Self {
            connection,
            lock_file,
        } = self;
        connection
            .execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")
            .map_err(|error| job_error("unable to checkpoint persistent job database", error))?;
        drop(connection);
        FileExt::unlock(&lock_file).map_err(|error| job_error("unable to unlock JOBDIR", error))?;
        Ok(())
    }
}
