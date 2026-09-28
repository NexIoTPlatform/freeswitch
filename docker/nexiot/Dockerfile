FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS builder

ARG BUILD_JOBS=2
ARG DEBIAN_MIRROR=deb.debian.org
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
    build-essential autoconf automake libtool libtool-bin pkg-config cmake yasm nasm \
    libsqlite3-dev libcurl4-openssl-dev libpcre2-dev libspeex-dev libspeexdsp-dev \
    libedit-dev libssl-dev uuid-dev libtiff-dev libpng-dev libfreetype-dev \
    libsndfile1-dev libshout3-dev libmpg123-dev libmp3lame-dev zlib1g-dev \
    libldns-dev libopus-dev libvpx-dev libjpeg-dev ca-certificates curl patch python3 \
    libavformat-dev libavcodec-dev libavutil-dev libswscale-dev libswresample-dev \
    && rm -rf /var/lib/apt/lists/*

COPY sources/ /sources/
COPY sources.lock.json /build-inputs/sources.lock.json
COPY context.manifest.json /build-inputs/context.manifest.json
RUN cd /sources && sha256sum -c sources.sha256 \
    && mkdir -p /build/sofia-sip /build/spandsp /build/freeswitch \
    && tar -xzf sofia-sip.tar.gz -C /build/sofia-sip --strip-components=1 \
    && tar -xzf spandsp.tar.gz -C /build/spandsp --strip-components=1 \
    && tar -xzf freeswitch.tar.gz -C /build/freeswitch --strip-components=1

WORKDIR /build/sofia-sip
RUN ./bootstrap.sh && ./configure --prefix=/usr/local --disable-dependency-tracking \
    && make -j"${BUILD_JOBS}" && make install && ldconfig

WORKDIR /build/spandsp
RUN ./bootstrap.sh && ./configure --prefix=/usr/local --disable-dependency-tracking \
    && make -j"${BUILD_JOBS}" && make install && ldconfig

WORKDIR /build/freeswitch
COPY modules.conf /build-inputs/modules.conf
RUN python3 tests/nexiot/test_rtp_headers.py src/switch_rtp.c \
    && ./bootstrap.sh -j \
    && cp /build-inputs/modules.conf modules.conf \
    && ./configure --prefix=/opt/freeswitch --disable-dependency-tracking
RUN make -j"${BUILD_JOBS}" && make install \
    && /opt/freeswitch/bin/freeswitch -version | grep -F 'FreeSWITCH version: @@FS_VERSION@@-release' \
    && dpkg-query -W > /build-inputs/builder-packages.tsv

FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
ARG DEBIAN_MIRROR=deb.debian.org
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
    ca-certificates libsqlite3-0 libcurl4 libpcre2-8-0 libspeex1 libspeexdsp1 libedit2 \
    libssl3 libuuid1 libtiff6 libpng16-16 libfreetype6 libsndfile1 libshout3 \
    libmpg123-0 libmp3lame0 zlib1g libldns3 libopus0 libvpx7 libjpeg62-turbo \
    libstdc++6 libatomic1 libavformat59 libavcodec59 libavutil57 libswscale6 libswresample4 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/ /usr/local/lib/
COPY --from=builder /opt/freeswitch/ /opt/freeswitch/
COPY --from=builder /build-inputs/ /usr/share/nexiot-freeswitch-build/
COPY --from=builder /build/freeswitch/tests/nexiot/ /usr/share/nexiot-freeswitch-build/tests/
RUN ldconfig \
    && ln -s /opt/freeswitch/bin/freeswitch /usr/bin/freeswitch \
    && ln -s /opt/freeswitch/bin/fs_cli /usr/bin/fs_cli \
    && /usr/bin/freeswitch -version \
    && for module in mod_console mod_logfile mod_commands mod_dptools mod_dialplan_xml \
       mod_sofia mod_event_socket mod_sndfile mod_shout mod_http_cache mod_tone_stream \
       mod_av mod_xml_curl; do \
       test -s "/opt/freeswitch/lib/freeswitch/mod/${module}.so" || { echo "Missing module: ${module}" >&2; exit 1; }; done \
    && if find /opt/freeswitch -type f -name '*.so*' -exec ldd {} \; 2>/dev/null | grep 'not found'; then exit 1; fi \
    && dpkg-query -W > /usr/share/nexiot-freeswitch-build/runtime-packages.tsv \
    && cp /etc/apt/sources.list.d/debian.sources /usr/share/nexiot-freeswitch-build/debian.sources

LABEL org.opencontainers.image.title="NexIoT FreeSWITCH" \
      org.opencontainers.image.version="@@IMAGE_VERSION@@" \
      org.opencontainers.image.revision="@@FS_COMMIT@@" \
      org.opencontainers.image.description="Built from the committed NexIoT FreeSWITCH source branch"
ENTRYPOINT ["/usr/bin/freeswitch"]
CMD ["-version"]
