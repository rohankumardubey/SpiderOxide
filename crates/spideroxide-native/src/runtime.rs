use std::cell::OnceCell;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Mutex, OnceLock};
use std::task::{Context, Poll};
use std::thread::{self, JoinHandle as ThreadJoinHandle};
use std::time::Duration;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3_async_runtimes::TaskLocals;
use pyo3_async_runtimes::generic::{
    self, ContextExt, JoinError as GenericJoinError, Runtime as GenericRuntime,
};
use tokio::runtime::{Builder, Handle};
use tokio::task::{self, JoinHandle};

const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);

struct RuntimeWrapper {
    handle: Handle,
    shutdown_sender: Mutex<Option<std::sync::mpsc::SyncSender<Duration>>>,
    runtime_thread: Mutex<Option<ThreadJoinHandle<()>>>,
}

impl RuntimeWrapper {
    fn new() -> Self {
        let (handle_sender, handle_receiver) = std::sync::mpsc::sync_channel(1);
        let (shutdown_sender, shutdown_receiver) = std::sync::mpsc::sync_channel(1);
        let runtime_thread = thread::Builder::new()
            .name("spideroxide-tokio-runtime".to_owned())
            .spawn(move || {
                let runtime = Builder::new_multi_thread()
                    .enable_all()
                    .build()
                    .expect("failed to build the SpiderOxide Tokio runtime");
                handle_sender
                    .send(runtime.handle().clone())
                    .expect("failed to publish the SpiderOxide Tokio runtime handle");
                let timeout = shutdown_receiver.recv().unwrap_or(SHUTDOWN_TIMEOUT);
                runtime.shutdown_timeout(timeout);
            })
            .expect("failed to start the SpiderOxide Tokio runtime thread");
        let handle = handle_receiver
            .recv()
            .expect("SpiderOxide Tokio runtime stopped during initialization");
        Self {
            handle,
            shutdown_sender: Mutex::new(Some(shutdown_sender)),
            runtime_thread: Mutex::new(Some(runtime_thread)),
        }
    }

    fn shutdown(&self) -> Result<bool, String> {
        let mut sender = self
            .shutdown_sender
            .lock()
            .map_err(|_| "native runtime shutdown state was poisoned")?;
        let Some(sender) = sender.take() else {
            return Ok(false);
        };
        sender
            .send(SHUTDOWN_TIMEOUT)
            .map_err(|_| "native runtime stopped before receiving the shutdown request")?;
        let thread = self
            .runtime_thread
            .lock()
            .map_err(|_| "native runtime thread state was poisoned")?
            .take();
        if let Some(thread) = thread {
            thread
                .join()
                .map_err(|_| "native runtime thread panicked during shutdown")?;
        }
        Ok(true)
    }
}

static RUNTIME: OnceLock<RuntimeWrapper> = OnceLock::new();

fn runtime() -> &'static RuntimeWrapper {
    RUNTIME.get_or_init(RuntimeWrapper::new)
}

struct RuntimeJoinError(task::JoinError);

impl GenericJoinError for RuntimeJoinError {
    fn is_panic(&self) -> bool {
        self.0.is_panic()
    }

    fn into_panic(self) -> Box<dyn std::any::Any + Send + 'static> {
        self.0.into_panic()
    }
}

struct RuntimeJoinHandle(JoinHandle<()>);

impl Future for RuntimeJoinHandle {
    type Output = Result<(), RuntimeJoinError>;

    fn poll(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Self::Output> {
        Pin::new(&mut self.0)
            .poll(context)
            .map_err(RuntimeJoinError)
    }
}

struct SpiderOxideRuntime;

tokio::task_local! {
    static TASK_LOCALS: OnceCell<TaskLocals>;
}

impl GenericRuntime for SpiderOxideRuntime {
    type JoinError = RuntimeJoinError;
    type JoinHandle = RuntimeJoinHandle;

    fn spawn<F>(future: F) -> Self::JoinHandle
    where
        F: Future<Output = ()> + Send + 'static,
    {
        RuntimeJoinHandle(runtime().handle.spawn(future))
    }

    fn spawn_blocking<F>(function: F) -> Self::JoinHandle
    where
        F: FnOnce() + Send + 'static,
    {
        RuntimeJoinHandle(runtime().handle.spawn_blocking(function))
    }
}

impl ContextExt for SpiderOxideRuntime {
    fn scope<F, R>(locals: TaskLocals, future: F) -> Pin<Box<dyn Future<Output = R> + Send>>
    where
        F: Future<Output = R> + Send + 'static,
    {
        let cell = OnceCell::new();
        cell.set(locals)
            .expect("SpiderOxide Tokio task locals were already initialized");
        Box::pin(TASK_LOCALS.scope(cell, future))
    }

    fn get_task_locals() -> Option<TaskLocals> {
        TASK_LOCALS
            .try_with(|cell| cell.get().cloned())
            .unwrap_or_default()
    }
}

pub(crate) fn future_into_py<F, T>(py: Python<'_>, future: F) -> PyResult<Bound<'_, PyAny>>
where
    F: Future<Output = PyResult<T>> + Send + 'static,
    T: for<'py> IntoPyObject<'py> + Send + 'static,
{
    generic::future_into_py::<SpiderOxideRuntime, _, T>(py, future)
}

#[pyfunction(name = "_shutdown_async_runtime")]
pub(crate) fn shutdown_async_runtime(py: Python<'_>) -> PyResult<bool> {
    let Some(runtime) = RUNTIME.get() else {
        return Ok(false);
    };
    py.detach(|| runtime.shutdown())
        .map_err(PyRuntimeError::new_err)
}
