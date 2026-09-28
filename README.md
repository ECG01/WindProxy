# WindProxy operational wind estimates

This project estimates neutral 10 m wind speed and direction from the
equilibrium tail of real-time CDIP wave spectra for:

- Arecibo, Puerto Rico (`249p1`)
- Rincon, Puerto Rico (`181p1`)

The estimator calculates the mean of `E(f) f^4` over `2 fp <= f <= 0.5 Hz`,
converts that level to air-side friction velocity, and numerically inverts
COARE 3.6 to obtain neutral `U10`.

## Wind direction

Most wind momentum enters the wave field at high frequency, so waves in the
equilibrium range travel close to the wind direction. Following Mudd et al.
(2024) Section 2.1.2, the reported direction is the mean wave direction
averaged across the same frequency band that produced the speed. That average
is the uniformly weighted circular mean of Voermans et al. (2020) equation 14:

    theta = atan2( mean(sin(theta_i)), mean(cos(theta_i)) )

A plain arithmetic mean is wrong here, since directions near north would average
to south. Directions use the meteorological convention and are reported as the
bearing the wind blows *from*, in degrees true.

CDIP publishes per-band `waveMeanDirection` as `sea_surface_wave_from_direction`
in degrees true, which is already the `theta(f)` of Voermans equation 13. Note
that CDIP's published `waveA1Value` and `waveB1Value` are pre-rotated, so
`waveMeanDirection` equals `atan2(b1, a1)` rather than the textbook
`270 - atan2(b1, a1)`. Deriving `theta(f)` from those coefficients with the
formula as written in the paper reverses the direction; use the published
`waveMeanDirection` instead.

Direction accuracy depends strongly on wind speed. Mudd et al. Table 2 reports a
direction RMSE of 13.2 degrees above 7 m/s against 56.2 degrees below it, so each
estimate carries a `direction_confidence` of `high` or `low` at that threshold.
Section 5 bounds the dependable speed range at 3 to 12 m/s, reported per record
as `wind_reliability`.

Directional quality is also reported directly. `direction_resultant_length` is
the circular concentration R over the band, near 1 when every band agrees and
near 0 when they disagree; `direction_circular_spread_deg` is the corresponding
circular standard deviation. Records whose band directions cancel outright
publish no direction and record the reason.

## Equilibrium band selection

By default the band is fixed at `2 fp <= f <= 0.5 Hz`. Passing
`--band-method adaptive` instead selects the band by the fitting method of Mudd
et al. Section 2.1.1: among contiguous segments above `2 fp` spanning 0.14 to
0.34 Hz, it takes the one whose `E(f) f^4` regression slope is closest to zero.
Frequency bands that CDIP flags as unreliable, such as those above the hull
response limit, are excluded from both methods.

The adaptive band tracks the equilibrium range far more closely. Measured over a
72-hour window at both stations, its log-log spectral slope has a median of
-4.00 against the -4.00 predicted by Phillips, and 84 percent (Arecibo) and 64
percent (Rincon) of records fall inside the -4.4 to -3.6 acceptance range of
Mudd et al. Section 2.1.3, against 39 and 34 percent for the fixed band.

Switching methods changes published `U10` values by up to 20 percent per record,
though the median is unchanged, so it is opt-in and warrants a
`--rebuild-history`. Direction is far less sensitive to the choice, differing by
a median of 1.6 degrees at Arecibo and 4.5 degrees at Rincon. Every record
records its `band_method` and `equilibrium_log_slope`, so the slope diagnostic
is available under either method; it only demotes `qc_status` under
`--band-method adaptive`, because the threshold is calibrated against an
adaptively selected band.

## Setup and manual run

Prerequisites:

- Python 3.9 or newer. The deployment runs 3.9.6; 3.11 or newer is a good
  default for a fresh install.
- macOS or Linux. The polling job serialises itself with `fcntl` file locking,
  which POSIX provides and Windows does not. Windows users can run it under WSL.
- Outbound HTTPS to `thredds.cdip.ucsd.edu`. CDIP's OPeNDAP service is open, so
  no account, API key, or credential of any kind is needed.

The setup script creates an isolated virtual environment; it does not require
Conda or a system-wide package install.

Clone the repository, then run:

```sh
git clone https://github.com/caose-lab-org/WindProxy.git
cd WindProxy
./scripts/setup_operational_wind.sh
./scripts/run_operational_wind.sh
```

If Python is not available as `python3`, point the setup script at it:

```sh
PYTHON3=/path/to/python3 ./scripts/setup_operational_wind.sh
```

Verify the install with the test suite, which needs no network:

```sh
.venv/bin/python -m unittest tests.test_operational_wind
```

The first run processes the most recent 48 hours. Later runs process only new
timestamps, with a small overlap to make retries safe. Simultaneous runs are
prevented with a local file lock.

A collaborator starting fresh gets an empty `data/operational/`, so their first
run seeds the last 48 hours and every run after that appends. Their record will
not match this deployment's until they run `--rebuild-history`, which reaches
back to `--history-start` from the CDIP archive. Nothing in the repository
depends on paths or state from the machine it was developed on.

Outputs are written under `data/operational/`:

- `wind_estimates.csv`: append-like historical product for both stations
- `wind_estimates_since_2026.csv`: dedicated record beginning January 1, 2026
- `latest.json`: newest usable estimate and staleness state by station
- `status.json`: health and record counts for the latest polling run

These products and logs are generated locally and intentionally are not stored
in the Git repository. A collaborator's first normal run begins with the most
recent 48 hours of available observations.

Rejected source observations remain in the historical CSV with reasons and no
published `U10`. A large equilibrium-tail coefficient of variation marks an
estimate `questionable` but retains it for evaluation.

Rows written before version 1.1.0 have empty direction columns; they are not
backfilled by normal polling. Use `--rebuild-history` to populate them for the
2026-onward record. From 1.1.0, `tail_min_hz` and `tail_max_hz` report the
lowest and highest frequency bin actually included in the band rather than the
theoretical `2 fp` and 0.5 Hz cutoffs.

To rebuild the dedicated 2026-onward record:

```sh
./scripts/run_operational_wind.sh --rebuild-history
```

Normal cron runs continue updating that file after the rebuild.

A rebuild reads the CDIP archive dataset as well as the real-time one, because
the real-time window is far too short to reach January. Its length also differs
per station: at the time of writing `249p1_rt.nc` reaches back about six months
while `181p1_rt.nc` covers about one week. Stations whose archive predates the
requested start simply contribute nothing, and overlapping timestamps prefer the
real-time value. Pass `--no-archive` to rebuild from real-time alone.

Because CDIP re-processes its archive, a rebuild can also correct earlier rows:
the 1.1.0 rebuild recovered six Rincon and Arecibo records that the real-time
feed had published with a zero wave height or a bad quality flag, and rejected
one that CDIP has since flagged. A short gap between the end of the archive and
the start of the real-time window leaves the occasional row unreprocessed.

## Plotting and validation

`scripts/build_wind_plot.py` charts the operational CSV against observed wind
from the nearest CARICOOS weather station, and writes for each buoy:

- `data/operational/wind_plot_<station>.html`: a self-contained interactive page
  (wind speed, wind direction, significant wave height, a buoy-vs-station
  scatter, and validation statistics). Open it directly in a browser.
- `data/operational/wind_plot_<station>.png`: the same figure for reports.

```sh
.venv/bin/python -m pip install -r requirements-plot.txt   # once, for the PNG
.venv/bin/python scripts/build_wind_plot.py
```

| Buoy | Validation station | Notes |
|---|---|---|
| Arecibo `249p1` | CARICOOS WindNet AROP4 | 6 minute m/s readings, anemometer 12 m above site; QARTOD suspect and failed readings are dropped |
| Rincon `181p1` | CARICOOS Tres Palmas `E9889_TPR` | 10 minute Davis readings published in mph and converted to m/s; sensor height not documented |

The CARICOOS WRF-NMM forecasts (the 1 km domain and the 2 km `d02` nest) are
scored in the same table and drawn on the same charts. Each hour comes from the
freshest 00Z or 12Z run at a 1 to 12 hour lead, falling back to an older run
when a file is missing. Each buoy uses its nearest model water cell and each
station its nearest cell, and the grid-relative NMM winds are rotated to
earth-relative. Only the handful of cells around the sites are downloaded, and
fetched hours are cached in `data/operational/model/`, so a polling run fetches
only the hours it has not seen; the first run of a new install catches up over
a few runs. Models are scored against the station hourly, independent of the
buoy, so the model validation continues while a buoy is offline. Pass
`--no-model` to skip it.

Station readings are averaged over each 30-minute buoy sample (direction as a
vector mean) and compared as bias (buoy minus station), RMSE, and correlation,
separately for `good` records and for every published record. Both stations
are land anemometers rather than open-water wind at 10 m, so a steady offset
against the neutral `U10` estimate is expected; timing and trend agreement are
the more telling check. Station data is read over plain OPeNDAP text with the
standard library, so only the PNG needs matplotlib.

By default the last seven days are plotted for both buoys. Pass `--station-id
249p1` for one buoy, `--days 0` for everything, `--input
data/operational/wind_estimates_since_2026.csv` for the 2026-onward record,
`--no-png` for HTML only, or `--no-validation` to skip the station fetch. A buoy
with no recent records, such as one under maintenance, still gets a page of
station observations with a notice.

## License and citation

Released under the MIT License; see `LICENSE`. The estimator implements
published methods, so work that uses it should cite the sources rather than
this repository alone:

> Mudd, K. C., A. Ho, A. Amador, J. Lodise, J. Behrens, and S. T. Merrifield
> (2024). Wind velocity estimates from wave observing platforms. *Coastal
> Engineering Journal* 66(3), 479-491. doi:10.1080/21664250.2024.2321660

> Voermans, J. J., et al. (2020). Estimating wind speed and direction using
> wave spectra. *Journal of Geophysical Research: Oceans* 125, e2019JC015717.

Wave data are provided by the Coastal Data Information Program (CDIP), Scripps
Institution of Oceanography, and are subject to CDIP's own terms of use.

## Repository layout

- `operational_wind.py`, `scripts/`, `tests/`: the deployable service.
- `experimental/`: Puerto Rico observations and comparison scripts, kept for
  validating estimates against the Arecibo weather station.
- `deprecated/`: superseded code and completed studies, kept for provenance.
  See `deprecated/README.md`.
- `literature/`: the papers the estimator implements.

Only the first of these is tracked in Git.

## Cron

A ten-minute polling interval catches a new 30-minute buoy product shortly
after CDIP publishes it. The script is idempotent, so polling more frequently
does not duplicate estimates.

Every run of `scripts/run_operational_wind.sh` updates the estimates and then
rebuilds the charts, validation, and `index.html` landing page in
`data/operational/`. A chart failure is logged but never blocks the estimates.
Set `WINDPROXY_PLOTS=0` to skip the charts.

```cron
*/10 * * * * /path/to/WindProxy/scripts/run_operational_wind.sh >> /path/to/WindProxy/logs/operational_wind.log 2>&1
```

Substitute the absolute path to your own checkout. `scripts/install_cron.sh`
fills it in for you and preserves any existing crontab.

Use absolute paths in cron. The wrapper uses the project's `.venv` directly,
so it does not depend on shell activation.

Install the entry while preserving the existing crontab with:

```sh
./scripts/install_cron.sh
```

## Publishing with NGINX

Pass a web folder to the installer and every run also copies the landing page,
charts, figures, and data products (`latest.json`, `status.json`, and both
CSVs) into it. Each file is replaced atomically, so NGINX never serves a
half-written page.

```sh
sudo mkdir -p /var/www/html/windproxy
sudo chown "$(id -un)" /var/www/html/windproxy
./scripts/install_cron.sh /var/www/html/windproxy
```

With NGINX's default site, whose root is `/var/www/html`, the dashboard is then
live at `http://<host>/windproxy/` with no configuration change. To serve it
from any other folder, add a location block and reload NGINX:

```nginx
location /windproxy/ {
    alias /srv/windproxy/;
    index index.html;
    # Products change every ten minutes; make browsers revalidate.
    add_header Cache-Control "no-cache";
}
```

Running the installer again with a different folder, or with none, replaces
the earlier entry. For a one-off publish without cron, set the variable
directly:

```sh
WINDPROXY_WEB_DIR=/var/www/html/windproxy ./scripts/run_operational_wind.sh
```
