"""
Start the Cloud-Based Urban Heat Island Prediction platform.

    python run.py                 # start the console on http://127.0.0.1:5000
    python run.py --reset         # wipe the emulated AWS account and start fresh
    python run.py --pipeline-only # provision + run the full pipeline, then exit
    python run.py --port 8080

On the very first start the end-to-end Step Functions pipeline runs automatically
in the background (about 30-60 seconds) so the dashboard fills itself with data.
"""
import argparse
import shutil
import sys
import webbrowser
from threading import Timer

from src import config


def main():
    parser = argparse.ArgumentParser(description=config.PROJECT_NAME)
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--reset", action="store_true", help="delete cloud_runtime/ (all emulated AWS state)")
    parser.add_argument("--pipeline-only", action="store_true", help="run the pipeline headless and exit")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = parser.parse_args()

    if args.reset and config.CLOUD_ROOT.exists():
        shutil.rmtree(config.CLOUD_ROOT)
        print("Emulated AWS account reset.")

    from src.backend import cloud_setup
    from src.aws_cloud import step_functions

    if args.pipeline_only:
        cloud_setup.provision()
        name = cloud_setup.run_pipeline("cli", wait=True)
        ex = step_functions.describe_execution(name)
        for s in ex["states"]:
            print(f"  {s['status']:<10} {s['name']:<18} {s['durationS'] or 0:>6}s")
        print(f"Pipeline {ex['status']}" + (f": {ex['error']}" if ex["error"] else ""))
        sys.exit(0 if ex["status"] == "SUCCEEDED" else 1)

    from src.backend.app import create_app
    app = create_app()

    if cloud_setup.needs_first_run():
        print("First start detected - running the end-to-end cloud pipeline in the background...")
        cloud_setup.run_pipeline("first-start bootstrap")

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '127.0.0.1') else args.host}:{args.port}"
    print("=" * 66)
    print(f"  {config.PROJECT_NAME}")
    print(f"  Cloud console : {url}")
    print(f"  Login         : {config.ADMIN_EMAIL}  /  {config.ADMIN_PASSWORD}")
    print(f"  Cloud mode    : {config.CLOUD_MODE}   region: {config.AWS_REGION}")
    print("=" * 66)
    if not args.no_browser:
        Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
