//! Runs slow work off the UI thread.
//!
//! The PocketBook backend cannot wake the UI thread from another thread, so the
//! result is collected by polling with a UI timer.

use std::sync::{Arc, Mutex};
use std::time::Duration;

const POLL_INTERVAL: Duration = Duration::from_millis(100);

/// Runs `work` on a new thread and passes its result to `done` on the UI thread.
pub fn run_in_background<T: Send + 'static>(
    work: impl FnOnce() -> T + Send + 'static,
    done: impl FnOnce(T) + 'static,
) {
    let slot = Arc::new(Mutex::new(None));
    let slot_for_work = slot.clone();
    std::thread::spawn(move || {
        let result = work();
        *slot_for_work.lock().unwrap() = Some(result);
    });
    poll(slot, done);
}

fn poll<T: 'static>(slot: Arc<Mutex<Option<T>>>, done: impl FnOnce(T) + 'static) {
    slint::Timer::single_shot(POLL_INTERVAL, move || {
        let result = slot.lock().unwrap().take();
        match result {
            Some(result) => done(result),
            None => poll(slot, done),
        }
    });
}
