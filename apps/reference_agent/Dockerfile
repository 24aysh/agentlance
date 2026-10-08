FROM python:3.12.12-alpine3.22@sha256:848ba4413eb897e225159b8fc1b02094576cbae4aa73fc13142608ae2c8c0e32
COPY apps/reference_agent/container_worker.py /worker.py
USER 65532:65532
ENTRYPOINT ["python", "-I", "-B", "/worker.py"]
