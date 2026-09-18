# jt2pkg - the JT worker. GPL-2.0 (it links Open CASCADE's TKJT reader, which is GPL).
#
# This binary runs ONLY as a subprocess behind the API and is NEVER linked into or shipped
# with anything we distribute. See the licensing rules in the repo README.
#
# Build (no sudo needed; uses the local OCCT build tree from docs/VENDOR.md):
#   cmake -S workers/jt -B workers/jt/build && cmake --build workers/jt/build -j$(nproc)
# The executable lands at workers/jt/build/jt2pkg, where the API looks for it by default.
# Override locations with -DTKJT_SRC=... -DOCCT_BUILD=... if your layout differs.

cmake_minimum_required(VERSION 3.16)
project(jt2pkg CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(TKJT_SRC "${CMAKE_CURRENT_SOURCE_DIR}/../../vendor/PyOpenJt/TKJT/src"
    CACHE PATH "TKJT reader sources (jriegel/PyOpenJt fork, git-ignored).
    This fork, not the original JT-Assistant one: it builds against modern OCCT
    and adds LZMA segment support, which JT v10 files need.")
set(OCCT_BUILD "${CMAKE_CURRENT_SOURCE_DIR}/../../vendor/occt-build"
    CACHE PATH "Local OCCT build tree (headers + libs, git-ignored)")

if(NOT EXISTS "${TKJT_SRC}/JtData/JtData_Model.hxx")
  message(FATAL_ERROR "TKJT sources not found at ${TKJT_SRC} - see docs/VENDOR.md")
endif()
if(NOT EXISTS "${OCCT_BUILD}/include/opencascade/Standard.hxx")
  message(FATAL_ERROR "OCCT build tree not found at ${OCCT_BUILD} - see docs/VENDOR.md")
endif()

# TKJT packages. JtToTopoDS is deliberately EXCLUDED: it converts Parasolid XT BRep
# geometry we do not need (JT carries authored tessellations, which is what we export),
# and excluding it keeps the OCCT linkage to TKernel + TKMath.
set(TKJT_PACKAGES JtAttribute JtData JtDecode JtElement JtNode JtProperty)
set(TKJT_SOURCES)
foreach(pkg ${TKJT_PACKAGES})
  file(GLOB srcs "${TKJT_SRC}/${pkg}/*.cxx")
  list(APPEND TKJT_SOURCES ${srcs})
  include_directories("${TKJT_SRC}/${pkg}")
endforeach()

# The headers live one level down: code says <Standard.hxx>, the file is
# <occt>/include/opencascade/Standard.hxx.
include_directories("${OCCT_BUILD}/include/opencascade")

find_package(TBB REQUIRED)
find_package(ZLIB REQUIRED)
find_package(LibLZMA REQUIRED)   # JT v10 compresses segments with LZMA

# TKJT_TRACE=ON: TKJT prints its internal parse trace to stderr (which object/segment
# fails). A diagnostic aid, never on in normal builds.
option(TKJT_TRACE "TKJT internal parse tracing to stderr" OFF)
if(TKJT_TRACE)
  add_compile_definitions(TKJT_CERRTRACING)
endif()

add_executable(jt2pkg jt2pkg.cpp ${TKJT_SOURCES})
target_include_directories(jt2pkg PRIVATE ${TBB_INCLUDE_DIRS} ${ZLIB_INCLUDE_DIRS}
                          ${LIBLZMA_INCLUDE_DIRS})
target_link_directories(jt2pkg PRIVATE "${OCCT_BUILD}/lin64/gcc/lib")
target_link_libraries(jt2pkg PRIVATE TKernel TKMath TBB::tbb ZLIB::ZLIB ${LIBLZMA_LIBRARIES})

# jtinfo: workstation diagnostic (same GPL boundary as jt2pkg).
add_executable(jtinfo jtinfo.cpp ${TKJT_SOURCES})
target_include_directories(jtinfo PRIVATE ${TBB_INCLUDE_DIRS} ${ZLIB_INCLUDE_DIRS}
                          ${LIBLZMA_INCLUDE_DIRS})
target_link_directories(jtinfo PRIVATE "${OCCT_BUILD}/lin64/gcc/lib")
target_link_libraries(jtinfo PRIVATE TKernel TKMath TBB::tbb ZLIB::ZLIB ${LIBLZMA_LIBRARIES})

# Run in place: bake the local OCCT lib dir into the binary so the API subprocess
# finds it without LD_LIBRARY_PATH.
set_target_properties(jt2pkg PROPERTIES
  BUILD_WITH_INSTALL_RPATH TRUE
  INSTALL_RPATH "${OCCT_BUILD}/lin64/gcc/lib"
  BUILD_RPATH "${OCCT_BUILD}/lin64/gcc/lib")
