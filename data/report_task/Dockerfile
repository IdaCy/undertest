FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS runtime
RUN useradd --create-home worker
WORKDIR /workspace
RUN chown worker:worker /workspace
USER worker
CMD ["tail", "-f", "/dev/null"]

FROM runtime AS checker

FROM runtime AS actor
COPY --chown=worker:worker report.py test_report.py /workspace/
