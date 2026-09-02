use std::fs;
use std::path::Path;
use std::time::Duration;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};

type CachedResponse = (f64, String, u16, Vec<(String, Vec<u8>)>, Vec<u8>);

fn cache_error(context: &str, error: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(format!("{context}: {error}"))
}

#[pyclass(module = "spideroxide._native", unsendable)]
pub(crate) struct NativeHttpCacheStore {
    connection: Option<Connection>,
}

#[pymethods]
impl NativeHttpCacheStore {
    #[new]
    fn new(path: &str) -> PyResult<Self> {
        let directory = Path::new(path);
        fs::create_dir_all(directory)
            .map_err(|error| cache_error("unable to create HTTP cache directory", error))?;
        let connection = Connection::open(directory.join("httpcache.sqlite3"))
            .map_err(|error| cache_error("unable to open HTTP cache database", error))?;
        connection
            .busy_timeout(Duration::from_secs(5))
            .map_err(|error| cache_error("unable to configure HTTP cache database", error))?;
        connection
            .execute_batch(
                "
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS responses (
                    fingerprint BLOB PRIMARY KEY,
                    stored_at REAL NOT NULL,
                    url TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    body BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS headers (
                    fingerprint BLOB NOT NULL,
                    position INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    value BLOB NOT NULL,
                    PRIMARY KEY (fingerprint, position),
                    FOREIGN KEY (fingerprint) REFERENCES responses(fingerprint)
                        ON DELETE CASCADE
                );
                ",
            )
            .map_err(|error| cache_error("unable to initialize HTTP cache database", error))?;
        Ok(Self {
            connection: Some(connection),
        })
    }

    fn retrieve(
        &mut self,
        fingerprint: &[u8],
        now: f64,
        expiration_secs: u64,
    ) -> PyResult<Option<CachedResponse>> {
        let transaction = self
            .connection
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("HTTP cache storage is closed"))?
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|error| cache_error("unable to start HTTP cache read", error))?;
        let cached = transaction
            .query_row(
                "
                SELECT stored_at, url, status, body
                FROM responses
                WHERE fingerprint = ?1
                ",
                params![fingerprint],
                |row| {
                    Ok((
                        row.get::<_, f64>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, Vec<u8>>(3)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| cache_error("unable to read HTTP cache response", error))?;
        let Some((stored_at, url, status, body)) = cached else {
            transaction
                .commit()
                .map_err(|error| cache_error("unable to finish HTTP cache read", error))?;
            return Ok(None);
        };
        if expiration_secs > 0 && stored_at + (expiration_secs as f64) < now {
            transaction
                .execute(
                    "
                    DELETE FROM responses
                    WHERE fingerprint = ?1 AND stored_at = ?2
                    ",
                    params![fingerprint, stored_at],
                )
                .map_err(|error| cache_error("unable to expire HTTP cache response", error))?;
            transaction
                .commit()
                .map_err(|error| cache_error("unable to commit HTTP cache expiration", error))?;
            return Ok(None);
        }
        let status = u16::try_from(status)
            .map_err(|_| PyValueError::new_err("cached HTTP status is out of range"))?;
        let headers = {
            let mut statement = transaction
                .prepare(
                    "
                SELECT name, value
                FROM headers
                WHERE fingerprint = ?1
                ORDER BY position ASC
                ",
                )
                .map_err(|error| cache_error("unable to prepare cached header read", error))?;
            statement
                .query_map(params![fingerprint], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?))
                })
                .map_err(|error| cache_error("unable to read cached headers", error))?
                .map(|row| {
                    row.map_err(|error| cache_error("unable to decode cached header", error))
                })
                .collect::<PyResult<Vec<_>>>()?
        };
        transaction
            .commit()
            .map_err(|error| cache_error("unable to finish HTTP cache read", error))?;
        Ok(Some((stored_at, url, status, headers, body)))
    }

    fn store(
        &mut self,
        fingerprint: &[u8],
        stored_at: f64,
        url: &str,
        status: u16,
        headers: Vec<(String, Vec<u8>)>,
        body: &[u8],
    ) -> PyResult<()> {
        let transaction = self
            .connection
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("HTTP cache storage is closed"))?
            .transaction()
            .map_err(|error| cache_error("unable to start HTTP cache transaction", error))?;
        transaction
            .execute(
                "
                INSERT INTO responses(fingerprint, stored_at, url, status, body)
                VALUES (?1, ?2, ?3, ?4, ?5)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    stored_at = excluded.stored_at,
                    url = excluded.url,
                    status = excluded.status,
                    body = excluded.body
                ",
                params![fingerprint, stored_at, url, i64::from(status), body],
            )
            .map_err(|error| cache_error("unable to store HTTP cache response", error))?;
        transaction
            .execute(
                "DELETE FROM headers WHERE fingerprint = ?1",
                params![fingerprint],
            )
            .map_err(|error| cache_error("unable to replace cached headers", error))?;
        for (position, (name, value)) in headers.into_iter().enumerate() {
            transaction
                .execute(
                    "
                    INSERT INTO headers(fingerprint, position, name, value)
                    VALUES (?1, ?2, ?3, ?4)
                    ",
                    params![fingerprint, position as i64, name, value],
                )
                .map_err(|error| cache_error("unable to store cached header", error))?;
        }
        transaction
            .commit()
            .map_err(|error| cache_error("unable to commit HTTP cache response", error))
    }

    fn remove(&mut self, fingerprint: &[u8]) -> PyResult<()> {
        self.connection
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("HTTP cache storage is closed"))?
            .execute(
                "DELETE FROM responses WHERE fingerprint = ?1",
                params![fingerprint],
            )
            .map_err(|error| cache_error("unable to remove HTTP cache response", error))?;
        Ok(())
    }

    fn __len__(&self) -> PyResult<usize> {
        self.connection
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("HTTP cache storage is closed"))?
            .query_row("SELECT COUNT(*) FROM responses", [], |row| row.get(0))
            .map_err(|error| cache_error("unable to count HTTP cache responses", error))
    }

    fn close(&mut self) -> PyResult<()> {
        let Some(connection) = self.connection.take() else {
            return Ok(());
        };
        connection
            .close()
            .map_err(|(_, error)| cache_error("unable to close HTTP cache database", error))
    }
}
