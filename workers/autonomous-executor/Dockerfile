# FA010 supplies an exact reviewed base digest and generated native guest lock.
# Build happens outside experiments, using a reviewed offline wheelhouse.
ARG EXECUTOR_BASE
FROM ${EXECUTOR_BASE}
COPY guest/requirements.guest.lock /build/requirements.guest.lock
COPY wheelhouse /build/wheelhouse
RUN python -m venv /opt/executor && /opt/executor/bin/python -m pip install --no-index --find-links=/build/wheelhouse --require-hashes -r /build/requirements.guest.lock
COPY fa_research_executor /opt/executor/lib/python3.12/site-packages/fa_research_executor
ENV PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["/opt/executor/bin/python", "-m", "fa_research_executor.guest"]
