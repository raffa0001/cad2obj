# Vendor sources (not committed)

`vendor/` holds third-party code and local native builds. It is git-ignored on
purpose: ~650 MB, nested git repos, and reproducible from the recipes below.
Everything here stays on the workstation that builds the JT worker.

## JT reader: JT-Assistant / TKJT (GPL-2.0)

The open JT reader from Open CASCADE. GPL, so it may only ever run as its own
process behind the API — never linked into or shipped with anything we
distribute (see README licensing rules).

- Source: https://github.com/Open-Cascade-SAS/JT-Assistant
- Pinned commit: `e859e8c` ("JT Assistant & JT Reader publication")
- Local copy: `vendor/JT-Assistant/` (library code is `vendor/JT-Assistant/TKJT/`)
- Reference only: `vendor/PyOpenJt/` (jriegel's fork, commit `21faef4`) — useful
  for its `JtTools/JtDump` usage example of the TKJT API. Not built.

## Open CASCADE 7.8.1 (LGPL-2.1 + exception)

TKJT needs OCCT headers + libs. The system OCCT (7.6.x, no dev package, no
sudo) is too old, so OCCT is built locally with only the modules TKJT needs.

- Source: https://github.com/Open-Cascade-SAS/OCCT
- Pinned: tag `V7_8_1` (commit `bd2a789f`)
- Local copy: `vendor/occt-src/`, build tree: `vendor/occt-build/`

Reproduce the build (no sudo needed):

    cd vendor
    git clone --branch V7_8_1 --depth 1 https://github.com/Open-Cascade-SAS/OCCT occt-src
    cmake -S occt-src -B occt-build \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_MODULE_ApplicationFramework=OFF \
      -DBUILD_MODULE_DETools=ON \
      -DBUILD_MODULE_DataExchange=ON \
      -DBUILD_MODULE_Draw=OFF \
      -DBUILD_MODULE_FoundationClasses=ON \
      -DBUILD_MODULE_ModelingAlgorithms=ON \
      -DBUILD_MODULE_ModelingData=ON \
      -DBUILD_MODULE_Visualization=OFF \
      -DBUILD_Inspector=OFF -DBUILD_RESOURCES=OFF
    cmake --build occt-build -j"$(nproc)"

Headers end up in `vendor/occt-build/include/opencascade`,
libs in `vendor/occt-build/lin64/gcc/lib`. The JT worker links against the
build tree directly (no `install` step needed).
