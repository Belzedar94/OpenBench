//! Calendar formatting of UTC timestamps (seconds since the epoch), the few
//! shapes v1 templates print with Django's `date` filter.

const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/// `(year, month 1-12, day, hour, minute, second)` of `secs` (UTC).
pub fn civil(secs: i64) -> (i64, u32, u32, u32, u32, u32) {
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day, (rem / 3600) as u32, ((rem / 60) % 60) as u32, (rem % 60) as u32)
}

/// Django `date:"Y-m-d H:i"`.
pub fn ymd_hm(secs: i64) -> String {
    let (y, m, d, hh, mm, _) = civil(secs);
    format!("{y:04}-{m:02}-{d:02} {hh:02}:{mm:02}")
}

/// Django `date:"Y-m-d"`.
pub fn ymd(secs: i64) -> String {
    let (y, m, d, ..) = civil(secs);
    format!("{y:04}-{m:02}-{d:02}")
}

/// Django `date:"j M"` / `"j M Y"` / `"j M H:i"` / `"j M Y H:i"`.
pub fn jm(secs: i64, year: bool, time: bool) -> String {
    let (y, m, d, hh, mm, _) = civil(secs);
    let mut s = format!("{d} {}", MONTHS[(m - 1) as usize]);
    if year {
        s.push_str(&format!(" {y}"));
    }
    if time {
        s.push_str(&format!(" {hh:02}:{mm:02}"));
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates() {
        // 2026-11-28 06:44 UTC
        let t = 1_795_848_240;
        assert_eq!(ymd_hm(t), "2026-11-28 06:44");
        assert_eq!(ymd(t), "2026-11-28");
        assert_eq!(jm(t, false, true), "28 Nov 06:44");
        assert_eq!(jm(t, true, false), "28 Nov 2026");
        assert_eq!(civil(0), (1970, 1, 1, 0, 0, 0));
    }
}
