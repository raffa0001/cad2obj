# Vendor sources (not committed)

`vendor/` holds third-party code and local native builds. It is git-ignored on
purpose: ~650 MB, nested git repos, and reproducible from the recipes below.
Everything here stays on the workstation that builds the JT worker.

## JT reader: TKJT from PyOpenJt (GPL-2.0) + JT-Assistant reference

The open JT reader from Open CASCADE. GPL, so it may only ever run as its own
process behind the API — never linked into or shipped with anything we
distribute (see README licensing rules).

- Built: https://github.com/jriegel/PyOpenJt, `TKJT/` subtree, pinned commit `21faef4`.
  This fork, not upstream: it builds against modern OCCT (no `MMgt_TShared`) and adds
  LZMA segment support, which JT v10 files need. `workers/jt` compiles it in directly.
- Reference only: https://github.com/Open-Cascade-SAS/JT-Assistant, commit `e859e8c`.
  Upstream 2015 code, does not build against OCCT 7.8. Kept for its `JTAssistant/`
  viewer, whose scene-graph traversal (`JTData_SceneGraph.cxx`) is the reference for
  `jt2pkg.cpp`'s walk. Not built.
- Samples: `vendor/PyOpenJt/Data/ExampleFiles/` (JT v8.0-v10.3, monolithic + shattered).
  Used by `jtinfo`/`jt2pkg` validation and as test inputs when present; never committed
  (see tests/conftest.py).

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
