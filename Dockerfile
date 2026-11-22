# Same base image the commits were labeled in: its Python decides which
# sqlglot tests run, and we run some of them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1

# The labeling environment: every package from requirements.lock.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# sqlglot at the pinned commit, straight from its GitHub repository.
COPY fetch_sqlglot.py /app/br/fetch_sqlglot.py
RUN python /app/br/fetch_sqlglot.py /app/sqlglot

# Our code (flat in the repo) goes to /app/br; committed maps to /app/br/maps.
COPY . /app/src
RUN mkdir -p /app/br/maps \
 && cp /app/src/*.py /app/src/durations.json /app/src/map_local.json.gz /app/br/ \
 && rm -f /app/br/score.py \
 && if [ -d /app/src/maps ]; then cp /app/src/maps/* /app/br/maps/ ; fi \
 && rm -rf /app/src

# Byte-compile everything so runs never pay for it, then build the test map
# (or take the committed one) and the run-time index.
RUN python -m compileall -q -j 4 /app/sqlglot/sqlglot /app/sqlglot/tests /app/br \
 && python /app/br/build_map.py /app/sqlglot \
 && python /app/br/build_index.py /app/br/maps/map.json.gz /app/sqlglot /app/br/index.pkl \
 && python -m compileall -q -j 4 /app/sqlglot/sqlglot /app/sqlglot/tests \
 && chmod -R a+rX /app

ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
WORKDIR /tmp
CMD ["python", "/app/br/selector.py"]
