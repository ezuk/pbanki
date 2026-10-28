use std::rc::Rc;
use std::time::Duration;

use slint::ComponentHandle;

use crate::MainWindow;

/// Wall-clock time in the device's time zone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LocalTime {
    pub hour: u32,
    pub minute: u32,
    pub second: u32,
}

/// Returns the current local time.
pub type ClockFn = Rc<dyn Fn() -> LocalTime>;

/// Reads the time with the C library, which applies the system time zone.
pub fn local_time() -> LocalTime {
    let now = unsafe { libc::time(std::ptr::null_mut()) };
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::localtime_r(&now, &mut tm) }.is_null() {
        return LocalTime {
            hour: 0,
            minute: 0,
            second: 0,
        };
    }
    LocalTime {
        hour: tm.tm_hour as u32,
        minute: tm.tm_min as u32,
        second: tm.tm_sec as u32,
    }
}

/// Formats hours and minutes, e.g. `14:05` or `2:05 PM`.
pub fn format_time(time: LocalTime, twelve_hour: bool) -> String {
    if !twelve_hour {
        return format!("{:02}:{:02}", time.hour, time.minute);
    }
    let suffix = if time.hour < 12 { "AM" } else { "PM" };
    let hour = match time.hour % 12 {
        0 => 12,
        hour => hour,
    };
    format!("{hour}:{:02} {suffix}", time.minute)
}

/// Shows the time on the UI and refreshes it at the start of each minute.
pub fn setup_clock(ui: &MainWindow, now: ClockFn, twelve_hour: bool) {
    tick(ui.as_weak(), now, twelve_hour);
}

fn tick(ui: slint::Weak<MainWindow>, now: ClockFn, twelve_hour: bool) {
    let Some(window) = ui.upgrade() else {
        return;
    };
    let time = now();
    window.set_clock(format_time(time, twelve_hour).into());
    let delay = Duration::from_secs(60 - u64::from(time.second.min(59)));
    slint::Timer::single_shot(delay, move || tick(ui, now, twelve_hour));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(hour: u32, minute: u32) -> LocalTime {
        LocalTime {
            hour,
            minute,
            second: 0,
        }
    }

    #[test]
    fn formats_24_hour_time() {
        assert_eq!(format_time(at(0, 5), false), "00:05");
        assert_eq!(format_time(at(14, 30), false), "14:30");
    }

    #[test]
    fn formats_12_hour_time() {
        assert_eq!(format_time(at(0, 5), true), "12:05 AM");
        assert_eq!(format_time(at(9, 41), true), "9:41 AM");
        assert_eq!(format_time(at(12, 0), true), "12:00 PM");
        assert_eq!(format_time(at(23, 59), true), "11:59 PM");
    }
}
