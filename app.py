"""Local webcam study monitor."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from math import hypot
from pathlib import Path
from time import monotonic
from typing import Any

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
SESSIONS = ROOT / "sessions"
SETTINGS = ROOT / "settings.txt"
WINDOW = "Focus Desk"

EYE_OPEN_THRESHOLD = 11.0  # scores below this count as closed eyes
HIDDEN_ALERT_SECONDS = 0.7  # face missing this long triggers the alert
CLOSED_ALERT_SECONDS = 0.6  # eyes closed this long triggers the alert
PHONE_CONFIDENCE = 0.5  # YOLO confidence needed for a phone box
PHONE_EVERY = 3  # run phone detection on every Nth frame (webcam mode)
CALIBRATION_SHARE = 0.75  # suggested threshold = share * your average score
MIN_CALIBRATION_SAMPLES = 15
MIN_SUMMARY_SECONDS = 1.0

ALERTS = {
    "hidden": ("Face out of view", "faudio.mp3", (224, 185, 148)),
    "sleep": ("Eyes closed", "alarm.mp3", (145, 178, 250)),
    "phone": ("Phone in view", "paudio.mp3", (180, 150, 249)),
}
ALERT_SHORT = {"phone": "phone", "sleep": "eyes", "hidden": "face"}


class SessionTracker:
    """Counts alert events and distracted time for one monitoring run."""

    def __init__(self) -> None:
        self.events: dict[str, int] = {key: 0 for key in ALERTS}
        self.seconds: dict[str, float] = {key: 0.0 for key in ALERTS}
        self.ratio_sum = 0.0
        self.ratio_count = 0
        self.alert: str | None = None
        self.since = 0.0
        self.last_now = 0.0

    def update(self, alert: str | None, now: float) -> None:
        """Record the alert state at time ``now``, counting events and durations."""
        self.last_now = now
        if alert == self.alert:
            return
        if self.alert is not None:
            self.seconds[self.alert] += now - self.since
        if alert is not None:
            self.events[alert] += 1
        self.alert = alert
        self.since = now

    def finish(self) -> None:
        """Close out the alert state still active at the end of the run."""
        if self.alert is not None:
            self.seconds[self.alert] += self.last_now - self.since
            self.alert = None

    def sample(self, ratio: float | None) -> None:
        """Feed one eye-openness reading into the session average."""
        if ratio is not None:
            self.ratio_sum += ratio
            self.ratio_count += 1

    @property
    def distracted(self) -> float:
        """Total seconds spent in any alert state."""
        return sum(self.seconds.values())

    @property
    def average_ratio(self) -> float | None:
        return self.ratio_sum / self.ratio_count if self.ratio_count else None

    def focus_score(self, elapsed: float) -> float:
        """Percent of ``elapsed`` spent with no alert active."""
        if elapsed <= 0:
            return 100.0
        return max(0.0, 100.0 * (elapsed - self.distracted) / elapsed)


def draw_dashboard(cv2, frame, ratio, alert, phones, elapsed, tracker=None):
    background = (27, 28, 32)
    surface = (41, 42, 48)
    text = (242, 244, 245)
    muted = (164, 173, 178)
    mint = (175, 223, 131)
    title, _, accent = ALERTS.get(alert, ("Monitoring", None, mint))
    hints = {
        "hidden": "Move back into frame.",
        "sleep": "Open your eyes when ready.",
        "phone": "Set your phone aside.",
    }
    height, width = frame.shape[:2]
    scale = min(624 / width, 458 / height)
    preview = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))))
    image_height, image_width = preview.shape[:2]
    left = 28 + (624 - image_width) // 2
    top = 136 + (458 - image_height) // 2
    canvas = cv2.copyMakeBorder(
        preview, top, 650 - top - image_height, left, 960 - left - image_width,
        cv2.BORDER_CONSTANT, value=background,
    )

    def write(message, x, y, size=0.5, color=text, thickness=1):
        cv2.putText(canvas, message, (x, y), cv2.FONT_HERSHEY_SIMPLEX, size, color, thickness, cv2.LINE_AA)

    cv2.line(canvas, (28, 103), (932, 103), surface, 1)
    cv2.circle(canvas, (43, 56), 15, mint, 2, cv2.LINE_AA)
    cv2.circle(canvas, (43, 56), 4, mint, -1, cv2.LINE_AA)
    write("Focus Desk", 70, 64, 1.0, text, 2)
    write("PERSONAL STUDY MONITOR", 72, 86, 0.36, muted)
    cv2.circle(canvas, (826, 61), 4, mint, -1, cv2.LINE_AA)
    write("CAMERA LIVE", 838, 66, 0.41, mint)
    cv2.rectangle(canvas, (27, 135), (652, 594), surface, 1)

    for box in phones.xyxy.tolist():
        x1, y1, x2, y2 = box
        cv2.rectangle(
            canvas, (left + int(x1 * scale), top + int(y1 * scale)),
            (left + int(x2 * scale), top + int(y2 * scale)), ALERTS["phone"][2], 2,
        )

    cv2.rectangle(canvas, (674, 136), (932, 594), surface, -1)
    cv2.rectangle(canvas, (674, 136), (932, 140), accent, -1)
    write("SESSION STATUS", 697, 178, 0.43, muted)
    write(title, 697, 218, 0.81, accent, 2)
    write(hints.get(alert, "Camera checks are active."), 697, 248, 0.45, text)
    cv2.line(canvas, (697, 278), (909, 278), muted, 1)
    write("ELAPSED", 697, 314, 0.43, muted)
    minutes, seconds = divmod(int(elapsed), 60)
    write(f"{minutes:02d}:{seconds:02d}", 697, 350, 0.9, text, 2)
    write("FACE", 697, 403, 0.43, muted)
    write("In frame" if ratio is not None else "No face", 697, 430, 0.55)
    write("PHONE", 815, 403, 0.43, muted)
    write("Detected" if len(phones) else "None", 815, 430, 0.55)
    write("EYE SCORE", 697, 480, 0.43, muted)
    write(f"{ratio:.1f}" if ratio is not None else "--", 697, 507, 0.55)
    write("FOCUS", 815, 480, 0.43, muted)
    focus = tracker.focus_score(elapsed) if tracker is not None else None
    write(f"{focus:.0f}%" if focus is not None else "--", 815, 507, 0.55)
    write("ALERTS", 697, 550, 0.43, muted)
    if tracker is not None:
        counts = "  ".join(f"{ALERT_SHORT[key]} {tracker.events[key]}" for key in ("phone", "sleep", "hidden"))
        write(counts, 697, 576, 0.45, muted)
    else:
        write("--", 697, 576, 0.45, muted)
    write("LOCAL CAMERA PREVIEW", 28, 624, 0.4, muted)
    write("Q / ESC TO EXIT", 804, 624, 0.42, muted)
    return canvas


def eye_ratio(face, width, height):
    def distance(first, second):
        a, b = face[first], face[second]
        return hypot((a.x - b.x) * width, (a.y - b.y) * height)

    eye_width = distance(130, 243)
    return 100 * distance(159, 145) / eye_width if eye_width else None


def warning(hidden_seconds, closed_seconds, phone_visible,
            hidden_limit=HIDDEN_ALERT_SECONDS, closed_limit=CLOSED_ALERT_SECONDS):
    if hidden_seconds >= hidden_limit:
        return "hidden"
    if closed_seconds >= closed_limit:
        return "sleep"
    if phone_visible:
        return "phone"
    return None


def load_settings() -> dict[str, float]:
    """Read tunable values from settings.txt, falling back to the defaults."""
    values = {
        "eye_threshold": EYE_OPEN_THRESHOLD,
        "hidden_seconds": HIDDEN_ALERT_SECONDS,
        "closed_seconds": CLOSED_ALERT_SECONDS,
    }
    if SETTINGS.is_file():
        for line in SETTINGS.read_text(encoding="utf-8").splitlines():
            key, sep, raw = line.partition("=")
            key = key.strip()
            if sep and key in values:
                try:
                    values[key] = float(raw)
                except ValueError:
                    pass
    return values


def save_settings(values: dict[str, float]) -> None:
    """Write tunable values to settings.txt, one ``key=value`` line each."""
    lines = [f"{key}={values[key]:g}" for key in sorted(values)]
    SETTINGS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def suggest_threshold(average: float) -> float:
    """Turn a calibrated average eye score into an alert threshold."""
    return max(1.0, round(average * CALIBRATION_SHARE, 1))


def save_session(tracker: SessionTracker, elapsed: float) -> Path:
    """Append one row describing the finished session to sessions/sessions.csv."""
    SESSIONS.mkdir(exist_ok=True)
    path = SESSIONS / "sessions.csv"
    average = tracker.average_ratio
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow([
                "ended", "duration_s", "focus_pct", "avg_eye_score",
                "phone_alerts", "phone_seconds",
                "eyes_closed_alerts", "eyes_closed_seconds",
                "face_hidden_alerts", "face_hidden_seconds",
            ])
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            f"{elapsed:.1f}",
            f"{tracker.focus_score(elapsed):.1f}",
            f"{average:.1f}" if average is not None else "",
            tracker.events["phone"], f"{tracker.seconds['phone']:.1f}",
            tracker.events["sleep"], f"{tracker.seconds['sleep']:.1f}",
            tracker.events["hidden"], f"{tracker.seconds['hidden']:.1f}",
        ])
    return path


def print_summary(tracker: SessionTracker, elapsed: float, log_path: Path | None = None) -> None:
    """Print the end-of-session summary to the terminal."""
    minutes, seconds = divmod(int(elapsed), 60)
    average = tracker.average_ratio
    print(
        f"\n{WINDOW} session summary\n"
        f"  Duration: {minutes:02d}:{seconds:02d}\n"
        f"  Phone alerts: {tracker.events['phone']} ({tracker.seconds['phone']:.1f}s)\n"
        f"  Eyes-closed alerts: {tracker.events['sleep']} ({tracker.seconds['sleep']:.1f}s)\n"
        f"  Face-hidden alerts: {tracker.events['hidden']} ({tracker.seconds['hidden']:.1f}s)\n"
        + (f"  Average eye score: {average:.1f}\n" if average is not None else "")
        + f"  Focus score: {tracker.focus_score(elapsed):.0f}%",
        flush=True,
    )
    if log_path is not None:
        print(f"  Saved to {log_path}", flush=True)


def report_session(args: argparse.Namespace, tracker: SessionTracker) -> None:
    """Save and summarize the session after monitoring ends."""
    elapsed = tracker.last_now
    if elapsed < MIN_SUMMARY_SECONDS:
        return
    log_path = None
    if not args.no_log:
        try:
            log_path = save_session(tracker, elapsed)
        except OSError:
            print("Could not save the session log.", flush=True)
    print_summary(tracker, elapsed, log_path)


def run(args: argparse.Namespace, settings: dict[str, float]) -> None:
    import pygame

    pygame.mixer.init()
    try:
        sounds = {
            key: pygame.mixer.Sound(str(ASSETS / filename))
            for key, (_, filename, _) in ALERTS.items()
        }
        channel = pygame.mixer.Channel(0)
        if args.test_sound:
            channel.play(sounds[args.test_sound])
            while channel.get_busy():
                pygame.time.wait(30)
            return
        monitor(args, settings, sounds, channel)
    finally:
        pygame.mixer.quit()


def calibrate(args: argparse.Namespace, settings: dict[str, float]) -> None:
    """Measure the user's open-eye score and save a personal threshold."""
    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    seconds = args.calibrate
    options = vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(ASSETS / "face_landmarker.task")),
        running_mode=vision.RunningMode.VIDEO,
    )
    capture = cv2.VideoCapture(args.video if args.video else args.camera)
    samples: list[float] = []
    try:
        if not capture.isOpened():
            raise RuntimeError("Could not open the camera or video source.")
        with vision.FaceLandmarker.create_from_options(options) as face_model:
            print(
                f"Calibration: look at the camera with your eyes open and relaxed for {seconds}s.",
                flush=True,
            )
            started = monotonic()
            frame_number = 0
            fps = capture.get(cv2.CAP_PROP_FPS) or 30
            last_timestamp = -1
            while True:
                ok, frame = capture.read()
                if not ok:
                    if args.video:
                        break
                    raise RuntimeError("The camera stopped sending frames.")
                now = monotonic() - started if not args.video else frame_number / fps
                frame_number += 1
                timestamp = max(last_timestamp + 1, int(now * 1000))
                last_timestamp = timestamp
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                faces = face_model.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp
                ).face_landmarks
                height, width = frame.shape[:2]
                if faces:
                    ratio = eye_ratio(faces[0], width, height)
                    if ratio is not None:
                        samples.append(ratio)
                remaining = seconds - now
                if not args.check:
                    state = "face detected" if faces else "no face found"
                    cv2.putText(
                        frame, f"CALIBRATION {max(0.0, remaining):.0f}s - {state}",
                        (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (175, 223, 131), 2, cv2.LINE_AA,
                    )
                    cv2.imshow(WINDOW, frame)
                    if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                        print("Calibration cancelled. No changes were made.", flush=True)
                        return
                if not args.video and remaining <= 0:
                    break
    finally:
        capture.release()
        if not args.check:
            cv2.destroyAllWindows()
    if len(samples) < MIN_CALIBRATION_SAMPLES:
        raise RuntimeError(
            "Could not see your face long enough. Check the lighting and camera angle, then try again."
        )
    average = sum(samples) / len(samples)
    threshold = suggest_threshold(average)
    values = dict(settings)
    values["eye_threshold"] = threshold
    save_settings(values)
    print(f"Average open-eye score: {average:.1f}", flush=True)
    print(f"New eye threshold: {threshold:g} ({CALIBRATION_SHARE:.0%} of your average) saved to settings.txt.", flush=True)
    print("Start Focus Desk again to use it, or override once with --eye-threshold.", flush=True)


def monitor(args: argparse.Namespace, settings: dict[str, float], sounds: dict[str, Any], channel: Any) -> None:
    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision
    from ultralytics import YOLO

    options = vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(ASSETS / "face_landmarker.task")),
        running_mode=vision.RunningMode.VIDEO,
    )
    model_path = ASSETS / "yolov8n.pt"
    if not model_path.is_file():
        raise RuntimeError("Missing assets/yolov8n.pt. Run Start Focus Desk.cmd to set up the app.")
    phone_model = YOLO(str(model_path))
    phone_every = args.phone_every if args.phone_every is not None else (1 if args.check else PHONE_EVERY)
    phone_conf = args.phone_conf if args.phone_conf is not None else PHONE_CONFIDENCE
    capture = cv2.VideoCapture(args.video if args.video else args.camera)
    tracker = SessionTracker()
    try:
        if not capture.isOpened():
            raise RuntimeError("Could not open the camera or video source.")
        with vision.FaceLandmarker.create_from_options(options) as face_model:
            hidden_since = closed_since = None
            playing = None
            phones = None
            started = monotonic()
            frame_number = 0
            fps = capture.get(cv2.CAP_PROP_FPS) or 30
            last_timestamp = -1
            while True:
                ok, frame = capture.read()
                if not ok:
                    if args.video:
                        break
                    raise RuntimeError("The camera stopped sending frames.")
                if args.crop:
                    x, y, width, height = args.crop
                    frame = frame[y:y + height, x:x + width]
                    if not frame.size:
                        raise RuntimeError("The crop falls outside the video.")
                now = frame_number / fps if args.video else monotonic() - started
                frame_number += 1
                timestamp = max(last_timestamp + 1, int(now * 1000))
                last_timestamp = timestamp
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                faces = face_model.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp
                ).face_landmarks
                height, width = frame.shape[:2]
                ratio = eye_ratio(faces[0], width, height) if faces else None
                if faces:
                    hidden_since = None
                elif hidden_since is None:
                    hidden_since = now
                if ratio is None or ratio >= settings["eye_threshold"]:
                    closed_since = None
                elif closed_since is None:
                    closed_since = now
                if phones is None or phone_every <= 1 or (frame_number - 1) % phone_every == 0:
                    phones = phone_model.predict(frame, classes=[67], conf=phone_conf, verbose=False)[0].boxes
                alert = warning(
                    now - hidden_since if hidden_since is not None else 0,
                    now - closed_since if closed_since is not None else 0,
                    len(phones) > 0,
                    hidden_limit=settings["hidden_seconds"],
                    closed_limit=settings["closed_seconds"],
                )
                tracker.update(alert, now)
                tracker.sample(ratio)
                if args.check:
                    if alert and alert != playing:
                        print(f"{now:.2f}s {alert}", flush=True)
                    playing = alert
                    continue
                if not channel.get_busy():
                    playing = alert
                    if playing:
                        channel.play(sounds[playing])
                dashboard = draw_dashboard(cv2, frame, ratio, alert or playing, phones, now, tracker)
                cv2.imshow(WINDOW, dashboard)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27) or cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
    finally:
        capture.release()
        if not args.check:
            cv2.destroyAllWindows()
        tracker.finish()
        report_session(args, tracker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--video", help="Use a video file in place of the webcam")
    parser.add_argument("--crop", type=int, nargs=4, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--check", action="store_true", help="Print detections from a video without opening a window")
    parser.add_argument("--test-sound", choices=ALERTS)
    parser.add_argument("--calibrate", type=int, nargs="?", const=5, metavar="SECONDS",
                        help="Measure your open-eye score for SECONDS and save a personal eye threshold")
    parser.add_argument("--eye-threshold", type=float,
                        help="Eye score below this counts as closed eyes (default 11; set by --calibrate)")
    parser.add_argument("--hidden-seconds", type=float,
                        help="Face out of view this long triggers the alert (default 0.7)")
    parser.add_argument("--closed-seconds", type=float,
                        help="Eyes closed this long triggers the alert (default 0.6)")
    parser.add_argument("--phone-conf", type=float,
                        help="Confidence needed for phone detection (default 0.5)")
    parser.add_argument("--phone-every", type=int,
                        help="Run phone detection every Nth frame to save CPU (default 3, or 1 with --check)")
    parser.add_argument("--no-log", action="store_true",
                        help="Do not save this session to sessions/sessions.csv")
    args = parser.parse_args()
    if args.check and not args.video:
        parser.error("--check requires --video")
    settings = load_settings()
    if args.eye_threshold is not None:
        settings["eye_threshold"] = args.eye_threshold
    if args.hidden_seconds is not None:
        settings["hidden_seconds"] = args.hidden_seconds
    if args.closed_seconds is not None:
        settings["closed_seconds"] = args.closed_seconds
    try:
        if args.calibrate is not None:
            calibrate(args, settings)
        elif args.check:
            monitor(args, settings, {}, None)
        else:
            run(args, settings)
    except (ImportError, OSError, RuntimeError) as error:
        parser.exit(1, f"{WINDOW}: {error}\n")


if __name__ == "__main__":
    main()
