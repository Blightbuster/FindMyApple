#!/bin/sh
# Spike 0b: can our requirements install and run inside the Home Assistant core container?
# Run via docker (see spike/README.md). /src is the repo's third_party/ mounted read-only.
# Prints one "RESULT key=value" line per check so the outcome is easy to grep.
set -u
arch=$(uname -m)
echo "RESULT arch=$arch"
python3 --version
python3 -c "import platform,sys; print('RESULT libc=' + '-'.join(platform.libc_ver()) + ' py=' + sys.version.split()[0])"
echo "RESULT cc=$(command -v gcc || command -v cc || echo none)"
echo "RESULT git=$(command -v git || echo none)"

work=/tmp/fma && rm -rf $work && mkdir -p $work
cp -r /src/findmy-parawanderer $work/findmy && rm -rf $work/findmy/.git
cp -r /src/machoadi-parawanderer $work/machoadi

# HA installs integration requirements with uv into the same environment; mimic that.
if command -v uv >/dev/null 2>&1; then PIP="uv pip install --system"; else PIP="python3 -m pip install"; fi
echo "RESULT installer=$PIP"

# 1. Does unicorn come as a prebuilt wheel for this platform? (--only-binary: no compiling allowed)
if $PIP --only-binary=:all: "unicorn>=2.1.4" >/tmp/unicorn.log 2>&1; then
  echo "RESULT unicorn_wheel=ok $(python3 -c 'import unicorn; print(unicorn.__version__)' 2>&1)"
else
  echo "RESULT unicorn_wheel=FAIL"; tail -5 /tmp/unicorn.log
fi

# 2. The full requirement set, as the manifest would list it.
if $PIP "pyicloud>=2.0" $work/findmy $work/machoadi >/tmp/all.log 2>&1; then
  echo "RESULT install_all=ok"
else
  echo "RESULT install_all=FAIL"; tail -15 /tmp/all.log
fi

# 3. Imports, and that HA itself still imports (no pin broken).
python3 - <<'EOF'
import importlib
for mod in ["unicorn", "pyicloud", "findmy", "findmy.icloud", "findmy.keychain", "findmy.cloudkit",
            "machoadi.mac_anisette", "homeassistant.core", "aiohttp", "cryptography", "bleak"]:
    try:
        m = importlib.import_module(mod)
        print(f"RESULT import_{mod}=ok {getattr(m, '__version__', '')}")
    except Exception as e:  # noqa: BLE001
        print(f"RESULT import_{mod}=FAIL {type(e).__name__}: {e}")
EOF
python3 -m pip check 2>&1 | head -20 | sed 's/^/pipcheck: /'

# 4. Does the emulator run here? Boot CoreADI under unicorn and ask whether the (fresh) machine is
#    provisioned. `info` never contacts Apple. /coreadi is spike/state/coreadi mounted read-only.
if [ -f /coreadi/CoreADI.x86_64 ]; then
  export COREADI_PATH=/coreadi/CoreADI.x86_64
  start=$(date +%s%N)
  if timeout 300 python3 -m machoadi.mac_anisette info --state /tmp/macstate >/tmp/info.log 2>&1; then
    echo "RESULT emulator_info=ok $(( ($(date +%s%N) - start) / 1000000 ))ms $(grep -o '"provisioned": [a-z]*' /tmp/info.log)"
  else
    echo "RESULT emulator_info=FAIL"; tail -15 /tmp/info.log
  fi
  /usr/bin/time -v true >/dev/null 2>&1 && TIMEV="/usr/bin/time -v" || TIMEV=""
  if [ -n "$TIMEV" ]; then
    $TIMEV python3 -m machoadi.mac_anisette info --state /tmp/macstate 2>&1 >/dev/null | grep -E "Maximum resident|Elapsed" | sed 's/^/RESULT emulator_/'
  fi
else
  echo "RESULT emulator_info=skipped (no /coreadi/CoreADI.x86_64)"
fi
