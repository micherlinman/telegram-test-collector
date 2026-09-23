#!/bin/bash
# Startet Collector und Webserver; endet einer der beiden, stoppt der Container.
python collector.py &
uvicorn web:app --host 0.0.0.0 --port 8000 &
wait -n
exit $?
