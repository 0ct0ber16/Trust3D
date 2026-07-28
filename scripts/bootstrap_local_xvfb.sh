#!/usr/bin/env bash
set -euo pipefail

ROOT=/224010104/Jerry
PROJECT=${ROOT}/trust3d
RUNTIME=${ROOT}/.local/xvfb
DEBS=${RUNTIME}/debs
EXTRACTED=${RUNTIME}/root
BIN=${RUNTIME}/bin
LOG_ROOT=${ROOT}/logs/runtime

mkdir -p "${DEBS}" "${EXTRACTED}" "${BIN}" "${LOG_ROOT}" "${ROOT}/.tmp"

packages=(
  fonts-dejavu-core libfontenc1 libfreetype6 libgl1 libglvnd0 libglx0
  libgl1-mesa-dri libglapi-mesa libglx-mesa0 libdrm2 libdrm-common
  libdrm-amdgpu1 libdrm-intel1 libdrm-nouveau2 libdrm-radeon1 libedit2
  libelf1 libexpat1 libffi8 libice6 libicu70 libllvm15 libpciaccess0
  libpixman-1-0 libsensors-config libsensors5 libsm6 libx11-6 libx11-data
  libx11-xcb1 libxau6 libxaw7 libxcb1 libxcb-dri2-0 libxcb-dri3-0
  libxcb-glx0 libxcb-present0 libxcb-randr0 libxcb-shm0 libxcb-sync1
  libxcb-xfixes0 libxdmcp6 libxext6 libxfixes3 libxfont2 libxkbfile1
  libxml2 libxmu6 libxmuu1 libxpm4 libxshmfence1 libxt6 libxxf86vm1
  mesa-utils mesa-utils-bin x11-common x11-xkb-utils xauth xfonts-base
  xfonts-encodings xfonts-utils xkb-data xserver-common xvfb
)

printf '开始在 %s 部署用户态 Xvfb。\n' "${RUNTIME}"
(
  cd "${DEBS}"
  apt-get download "${packages[@]}"
) 2>&1 | tee "${LOG_ROOT}/xvfb-download.log"

for package in "${DEBS}"/*.deb; do
  dpkg-deb -x "${package}" "${EXTRACTED}"
done

cp "${EXTRACTED}/usr/bin/Xvfb" "${BIN}/Xvfb.tmp"
python - <<'PY'
from pathlib import Path

path = Path("/224010104/Jerry/.local/xvfb/bin/Xvfb.tmp")
data = path.read_bytes()
old = b"/usr/bin\0"
if data.count(old) != 1:
    raise SystemExit("Xvfb 中 /usr/bin 路径的数量不符合预期")
path.write_bytes(data.replace(old, b"." + b"\0" * (len(old) - 1)))
PY
chmod 0755 "${BIN}/Xvfb.tmp"
mv "${BIN}/Xvfb.tmp" "${BIN}/Xvfb"

cp "${EXTRACTED}/usr/bin/xkbcomp" "${BIN}/xkbcomp.bin"
chmod 0755 "${BIN}/xkbcomp.bin"
cat > "${PROJECT}/xkbcomp.tmp" <<'EOF'
#!/bin/sh
exec /224010104/Jerry/.local/xvfb/bin/xkbcomp.bin \
  -I/224010104/Jerry/.local/xvfb/root/usr/share/X11/xkb "$@"
EOF
chmod 0755 "${PROJECT}/xkbcomp.tmp"
mv "${PROJECT}/xkbcomp.tmp" "${PROJECT}/xkbcomp"

export PATH="${BIN}:${EXTRACTED}/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${EXTRACTED}/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export LIBGL_DRIVERS_PATH="${EXTRACTED}/usr/lib/x86_64-linux-gnu/dri"
export LIBGL_ALWAYS_SOFTWARE=1
export TMPDIR=${ROOT}/.tmp
error_log=${LOG_ROOT}/xvfb-bootstrap-test.err
: > "${error_log}"
xvfb-run -a -e "${error_log}" \
  --server-args="-screen 0 1280x1024x24 -nolisten tcp -xkbdir ${EXTRACTED}/usr/share/X11/xkb -fp ${EXTRACTED}/usr/share/fonts/X11/misc,${EXTRACTED}/usr/share/fonts/X11/Type1" \
  bash -c 'glxinfo -B | grep -E "OpenGL renderer|string: 4\\.[0-9]"'

if [[ -s ${error_log} ]]; then
  printf '本地 Xvfb 测试产生错误日志：%s\n' "${error_log}"
  exit 1
fi
printf '用户态 Xvfb 部署完成。\n'
