# Performance avenues

Where the CPU goes in the dashboard, what this branch already changed, and
which further changes are worth their risk on the deployed hardware. This is
an engineering note, not user documentation; the README stays the operator
guide.

## The constraints that decide everything

- **Hardware.** A Raspberry Pi (4 or 5 class), three USB 2.0 UVC cameras at
  640x480 MJPEG, one small display, vehicle power, and heat: the enclosure
  sits in a cab or cargo bay and the CPU throttles itself at 80-85 C. The
  dynamic-FPS controller lowers rates at 75 C to stay ahead of that.
- **Process shape.** One Python process: a Qt UI thread plus one `QThread`
  per camera. OpenCV releases the GIL inside `grab()`/`retrieve()`, so the
  three decodes run in parallel; everything else contends for the GIL.
- **Pipeline.** USB -> `v4l2src` -> `jpegdec` (software) -> `videoconvert`
  -> `appsink` -> OpenCV `retrieve()` (one copy into a fresh NumPy array)
  -> queued Qt signal -> UI thread styles the frame, converts to `QPixmap`,
  scales into the tile, `QLabel` paints. The V4L2 fallback does the JPEG
  decode inside `retrieve()` instead of inside GStreamer.
- **Field constraint.** The code is in daily use. A change that is faster
  but crashes once a week is a regression. Every avenue below is graded on
  risk first.

## What this branch changed, with numbers

Measured on an x86 laptop (Python 3.14, OpenCV 5.0, Qt 6.11, offscreen
platform). Absolute times on a Pi 4 are roughly 8-15x larger; the ratios
are what matter. Reproduce with `benchmarks/bench_render.py` and the
scratch comparison in the commit history.

| Path (640x480 BGR frame) | Before | After | Note |
| --- | ---: | ---: | --- |
| Brightness 1.5, convert to pixmap | 0.45 ms | 0.11 ms | one 3-channel LUT instead of three strided per-channel LUTs |
| Night mode, convert to pixmap | 0.50 ms | 0.08 ms | 8-bit indexed image with red palette instead of a BGR buffer |
| Night + brightness | 0.50 ms | 0.08 ms | tables composed once |
| Frame hand-off worker -> UI | 16 us + 2 locks | 0 | pool removed; `retrieve()` already allocates |
| Emitted rate, 25 FPS camera, 25 FPS target | ~16 FPS | ~25 FPS | deadline throttle instead of phase-reset |

The throttle number is the one that changes what the driver sees: with the
default profile the old code delivered roughly two thirds of the configured
rate to the screen. The render numbers matter under stress, when a Pi core
is already saturated and every 0.1 ms per frame per tile counts.

Correctness fixes that fell out of the rewrite (each is in a commit message):
brightness compounding on re-render, hot-plug attach never completing
(`QTimer.singleShot` from a non-Qt thread), the rescan timer stopping and
taking the detach check with it, and the restart budget's unreachable
recovery branch.

## Where the remaining CPU goes

Per camera per second at the default profile (25 FPS capture, 20 FPS UI),
in order of cost on a Pi:

1. **MJPEG decode.** 25 decodes/s of 640x480. This is the bulk of the
   load, and it happens for every frame the camera sends regardless of
   whether the dashboard wants it (GStreamer path) or only for frames that
   pass the throttle (V4L2 path, after this branch).
2. **Colour conversion and copies.** `videoconvert` (I420 -> BGR), the
   `appsink` pull, OpenCV's copy into a NumPy array, Qt's `QImage` ->
   `QPixmap` conversion, and the scale into the tile. Four full-frame
   passes; each is ~1 ms on a Pi.
3. **UI thread overhead.** Timer wakeups (20/s per tile), signal delivery,
   `QLabel` repaint, compositor blit. Small individually, but all serialised
   on one thread behind the GIL.
4. **Everything else.** Health logging, status lines, the rescan tick,
   `is_system_stressed()` reading two files every 2 s: noise.

## Avenues, ranked

Ordering weighs expected gain against risk to a fielded system and the
effort to validate on the Pi. "Validate" means the checklist at the end.

### 1. Ask the camera for the frame rate you will use (high gain, low risk)

On the GStreamer path the camera runs at its native rate (usually 30 FPS)
and `jpegdec` decodes all of it; the dashboard then throws a third away.
Adding `framerate=25/1` to the `image/jpeg` caps (and lowering it when
dynamic FPS lowers the target, which currently only throttles in software)
makes the camera send fewer frames, so the decode cost drops in proportion.
Under stress at the 10 FPS floor that is 60% of the decode work gone, on
exactly the machine that needs it.

Risk: some UVC cameras only advertise a few discrete rates and the caps
negotiation fails. Ship it as another rung in the existing fallback chain
(with-rate, then without), and log which one won. Changing the rate at
runtime means rebuilding the pipeline; do it only on the dynamic-FPS step
boundaries and only if the worker is otherwise healthy, or accept
software-only throttling below the initial rate. `bench_capture.py` shows
the effect directly (source FPS vs CPU%).

The V4L2 fallback already does this via `CAP_PROP_FPS`; the benchmark on
the laptop webcam shows the source rate following the target.

### 2. Hardware JPEG decode on Pi 4 (high gain, must be proven per unit)

Pi 4 exposes the VideoCore JPEG decoder as a V4L2 M2M device and GStreamer
has `v4l2jpegdec` for it. Swapping `jpegdec` for `v4l2jpegdec` moves the
dominant cost off the ARM cores. Pi 5 dropped that block, so this is a
per-model option, not a default.

Risk: driver stability over multi-day uptimes with three concurrent streams
is unproven, and the M2M device is shared, so three pipelines contend for
it. Same pattern as avenue 1: an opt-in config key, fallback to `jpegdec`
when the element is missing or the pipeline fails to preroll, and a 24-hour
soak before it ships. Not worth attempting on Pi 5.

### 3. Match capture resolution to what is displayed (medium gain, config only)

Decode and every copy scale with pixel count. In the 2x2 grid on a
1024x600 or 1280x800 panel each tile is roughly 512x300 to 640x400, so a
640x480 capture is already being downscaled; 320x240 would cut decode cost
by ~4x at a visible quality loss in fullscreen. The right call depends on
how much fullscreen is used in the cab and how small an obstacle must be
recognisable. This is a `config.ini` decision that needs numbers from
`bench_render.py` and `bench_capture.py` on the actual unit, not code.

A hybrid (capture 640x480, but tell GStreamer to scale to the tile size
with `videoscale` before `appsink`) trades a cheap GPU-less scale in C for
the Qt scale in the UI thread. Modest, and it interacts with fullscreen.
Lower priority.

### 4. Paint frames directly instead of through QLabel (small-medium gain, low risk)

`_present()` converts the styled `QImage` to a `QPixmap`, draws it scaled
into a cached tile-sized `QPixmap`, and hands that to `QLabel`, which paints
it again. Overriding `paintEvent` on the tile and calling
`painter.drawImage(rect, image)` skips one full-frame conversion and one
copy per render. On a Pi that is around 1-2 ms per frame per tile: 6-12% of
a core at three tiles and 20 FPS.

Risk is low (pure UI-thread change, no threading), but it touches the
fullscreen overlay and placeholder rendering, so it needs the gesture and
fullscreen checklist. Measure with `bench_render.py` (the `scale to tile`
row is what disappears) before deciding.

### 5. Replace the 1-minute load average in the stress controller (correctness of control, tiny cost)

`os.getloadavg()[0]` has a one-minute time constant; the controller polls
every 2 s and needs three consecutive hits, so it reacts to a CPU spike
roughly a minute late and keeps FPS lowered a minute after the cause is
gone. Loadavg also counts tasks in uninterruptible I/O wait, not just CPU.
Reading `/proc/stat` and computing busy percentage over the poll interval
gives an honest, immediate signal at no cost. Temperature is already read
directly and is fine.

This is not a CPU saving; it makes the existing mechanism do what its
config comments claim. Low risk, small change, verify by watching
`Stress detected` / `Restoring FPS` lines against `vcgencmd measure_temp`
under a deliberate load.

### 6. Emit no faster than the UI renders (small gain, small latency trade)

The worker emits at the capture target (25) while the tile renders at 20;
five frames per second per camera are retrieved, copied, signalled, and
never shown. Emitting at `min(capture_target, ui_fps)` removes that work.
The cost is up to one render interval of extra latency variance because
the two clocks are no longer oversampled. For a blind-spot display latency
is the metric that matters more than CPU, so this is a knob, not a
default.

### 7. Skip metadata nodes during discovery (startup time, low risk)

Each UVC camera creates two `/dev/video*` nodes; the metadata node fails
to open, and discovery retries it three times with 0.2 s sleeps, then
spawns `lsof`/`fuser` to evict a holder that does not exist, then the
confirmation pass repeats part of that. With three cameras that is a few
seconds of boot latency before the first frame. Filtering by V4L2 device
capabilities (`v4l2-ctl --device=/dev/videoN --info`, or the
`VIDIOC_QUERYCAP` ioctl directly) to only probe nodes with
`Video Capture`, or preferring `/dev/v4l/by-id/*-video-index0`, avoids the
wasted probes. Safe, easy, and it shortens the window where the driver
sees no camera on the dashboard after a reboot or a settings restart.

### 8. Frame allocation churn (uncertain, measure first)

Every retrieved frame is a fresh 921 KB allocation. glibc serves blocks
that size with `mmap` until its dynamic threshold grows, so early in the
process each frame costs page faults on first touch. After warm-up the
threshold adapts and the cost disappears. Worth a look at RSS and minor
faults over the first minutes on the Pi (`/proc/<pid>/stat` fields 10 and
24). If it shows up, `MALLOC_MMAP_THRESHOLD_=2097152` in the service
environment is a one-line fix. Do not reintroduce a frame pool: the
receiver cannot return the buffer OpenCV allocated, so the pool only ever
added a copy.

### 9. Run without a compositor (real gain on a kiosk, medium effort)

Under Wayland or X11 every repaint goes through a compositor that blits
the full screen again. The Qt `eglfs` platform draws straight to KMS and
removes that layer. On a 4-core Pi the compositor's share is not in our
process but competes with it. The catch: `eglfs` handles one top-level
window well, and the fullscreen view is currently a second top-level
window. It would have to become a stacked widget inside the main window
first. Touch input works through libinput. This is a deployment change
with an install-script and validation cost, so it belongs after 1-5.

### 10. One process per camera (robustness more than speed, high effort)

A capture thread wedged in a driver call cannot be stopped from Python;
today the dashboard leaves the thread alone and eventually detaches the
tile. Isolating each camera in its own process (frames via shared memory,
supervised by the dashboard) turns a wedged driver into a process kill and
respawn, and lets the UI keep running through a decoder crash. It also
sidesteps the GIL entirely. It is a redesign, with new failure modes of
its own (shared-memory lifetime, orphaned processes on restart, the
`execv` restart path). Worth it only if the field logs show wedged workers
are a recurring cause of blank tiles.

## Not worth it

- **JIT or Cython for the pixel work.** It is already a handful of OpenCV
  and Qt calls in C; Python overhead in the render path is negligible.
- **A GPU-composited video widget (QOpenGLWidget / RHI).** On the Pi's
  vc4 driver the upload of a CPU-decoded frame costs as much as the CPU
  paint it replaces, and it adds driver risk.
- **asyncio or a different threading model for the UI.** The UI thread is
  not the bottleneck; decode is.
- **YUYV capture to skip JPEG decode.** Three 640x480 YUYV streams at 25
  FPS need about 46 MB/s, and the Pi's USB 2.0 cameras share one 480 Mbit/s
  bus. It does not fit; at 320x240 it does, which folds into avenue 3.

## How to validate any of the above on a unit

1. Baseline first: run `benchmarks/bench_render.py` and
   `benchmarks/bench_capture.py --seconds 20` on the Pi, and record CPU
   temperature and `top` for the dashboard at three cameras after ten
   minutes.
2. Apply one change at a time, behind a config key defaulting to the old
   behaviour, with the old path as the fallback in the chain.
3. Repeat the benchmarks, then run the app for 24 hours at `WARNING` log
   level and watch for: `Stale frame detected`, `Restart limit reached`,
   `thread did not stop`, `Killing holders of`, and any GStreamer warnings
   on stderr.
4. Exercise the transitions, not just uptime: unplug and replug each
   camera, use the settings-tile restart, toggle fullscreen and night mode
   at 150% brightness, and run one hot cycle with the enclosure closed.
5. Only then flip the default.
