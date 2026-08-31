"""Background job worker (SPEC §5.1 `worker` service)."""

import time

from django.core.management.base import BaseCommand

from homeautoshop.core.jobs import drain
from homeautoshop.core.runtime import ensure_overlay, is_stale
from homeautoshop.core.schedule import tick


class Command(BaseCommand):
    help = "Run queued background jobs until interrupted."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Drain the queue once and exit.")
        parser.add_argument("--interval", type=float, default=2.0)
        parser.add_argument(
            "--no-schedule",
            action="store_true",
            help="Do not enqueue recurring jobs; drain only.",
        )

    def handle(self, *args, **options):
        scheduling = not options["no_schedule"]
        # Records which configuration this process is running, so it can notice
        # when that stops being true (§17.2).
        ensure_overlay()
        if options["once"]:
            queued = tick() if scheduling else 0
            self.stdout.write(f"queued {queued}, ran {drain()} job(s)")
            return

        self.stdout.write("worker started")
        # Recurring work is enqueued here rather than by cron or a beat
        # process, so "is the nightly backup running" is answerable from the
        # health screen instead of from inside the container (see schedule.py).
        # Checked once a minute: the queue drain runs far more often, and
        # asking every two seconds would be a query per tick for nothing.
        last_scheduled = 0.0
        while True:
            try:
                now = time.monotonic()
                if scheduling and now - last_scheduled >= 60:
                    tick()
                    last_scheduled = now
                    # A restart-class setting has been changed since this
                    # process booted, so it is running stale configuration.
                    # Exiting is the whole mechanism: compose has
                    # `restart: unless-stopped`, so it comes back within
                    # seconds with the new values — no signal to deliver
                    # across containers, and nothing to build (§17.2).
                    if is_stale():
                        self.stdout.write("configuration changed; restarting")
                        return
                if not drain():
                    time.sleep(options["interval"])
            except KeyboardInterrupt:
                self.stdout.write("worker stopped")
                return
