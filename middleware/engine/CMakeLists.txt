# Standalone build of the cico rewrite engine against an existing NeuG build.
# It links the prebuilt libneug.so and never rebuilds NeuG itself.
cmake_minimum_required(VERSION 3.16)
project(incgq_cico_engine CXX)

# The middleware lives outside the IncGQ tree (neo4j/middleware); NeuG is
# found through INCGQ_ROOT or -DNEUG_SOURCE_DIR.
if(DEFINED ENV{INCGQ_ROOT})
  set(_incgq_default "$ENV{INCGQ_ROOT}")
else()
  get_filename_component(_incgq_default "${CMAKE_CURRENT_SOURCE_DIR}/../../../IncGQ" ABSOLUTE)
endif()
set(NEUG_SOURCE_DIR "${_incgq_default}" CACHE PATH "IncGQ (NeuG) repository root")
set(NEUG_BUILD_DIR "${NEUG_SOURCE_DIR}/build" CACHE PATH "Existing NeuG build directory")
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

if(NOT EXISTS "${NEUG_BUILD_DIR}/src/libneug.so")
  message(FATAL_ERROR "libneug.so not found under ${NEUG_BUILD_DIR}/src; build NeuG first")
endif()

set(_deps ${NEUG_BUILD_DIR}/_deps/arrow-build)
add_executable(incgq_rewrite_engine incgq_rewrite_engine.cc)
# Test-only: executes Cypher on a NeuG database for end-to-end comparisons.
add_executable(cico_exec cico_exec.cc)
set(_targets incgq_rewrite_engine cico_exec)

# Kernel-patch testing without rebuilding libneug.so: sources listed in
# CICO_INTERPOSE_SOURCES (e.g. a patched rewriter_v4.cpp from a worktree) are
# compiled into incgq_rewrite_engine_patched and cico_exec_patched; with -rdynamic their definitions
# interpose libneug's.  Only valid when headers are unchanged from the build.
set(CICO_INTERPOSE_SOURCES "" CACHE STRING "Patched NeuG sources to interpose")
option(CICO_BUNDLED_PATCH "Apply bundled rewrite fixes to the current NeuG sources" OFF)
if(CICO_BUNDLED_PATCH)
  if(CICO_INTERPOSE_SOURCES)
    message(FATAL_ERROR "Choose CICO_BUNDLED_PATCH or CICO_INTERPOSE_SOURCES, not both")
  endif()
  find_package(Python3 REQUIRED COMPONENTS Interpreter)
  set(_patch "${CMAKE_CURRENT_SOURCE_DIR}/../../implementation/middleware/patches/neug-rootpost-group-coverage.patch")
  set(_prepared "${CMAKE_CURRENT_BINARY_DIR}/patched/rewriter_v4.cpp")
  set(_distinct_patch "${CMAKE_CURRENT_SOURCE_DIR}/../../implementation/middleware/patches/neug-unrelated-distinct-count.patch")
  set(_predicate_patch "${CMAKE_CURRENT_SOURCE_DIR}/../../implementation/middleware/patches/neug-temporal-predicate-keys.patch")
  set(_predicate_prepared "${CMAKE_CURRENT_BINARY_DIR}/patched/index_predicate_key.cpp")
  set(_count_patch "${CMAKE_CURRENT_SOURCE_DIR}/../../implementation/middleware/patches/neug-total-distinct-count.patch")
  set(_count_prepared "${CMAKE_CURRENT_BINARY_DIR}/patched/complete_key_group_rewriter.cpp")
  set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${_patch}" "${_distinct_patch}" "${_predicate_patch}" "${_count_patch}" "${CMAKE_CURRENT_SOURCE_DIR}/prepare_patch.py"
    "${NEUG_SOURCE_DIR}/src/compiler/binder/rewriter/rewriter_v4.cpp"
    "${NEUG_SOURCE_DIR}/src/compiler/binder/expression/index_predicate_key.cpp"
    "${NEUG_SOURCE_DIR}/src/compiler/binder/rewriter/complete_key_group_rewriter.cpp")
  execute_process(COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/prepare_patch.py"
    --neug-source "${NEUG_SOURCE_DIR}" --patch "${_patch}" --patch "${_distinct_patch}" --output "${_prepared}"
    RESULT_VARIABLE _patch_result)
  if(NOT _patch_result EQUAL 0)
    message(FATAL_ERROR "Cannot apply bundled patch to the current NeuG source; rebase its contexts")
  endif()
  execute_process(COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/prepare_patch.py"
    --neug-source "${NEUG_SOURCE_DIR}" --patch "${_predicate_patch}" --output "${_predicate_prepared}"
    --source "src/compiler/binder/expression/index_predicate_key.cpp"
    RESULT_VARIABLE _predicate_result)
  if(NOT _predicate_result EQUAL 0)
    message(FATAL_ERROR "Cannot apply temporal predicate patch to the current NeuG source")
  endif()
  execute_process(COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/prepare_patch.py"
    --neug-source "${NEUG_SOURCE_DIR}" --patch "${_count_patch}" --output "${_count_prepared}"
    --source "src/compiler/binder/rewriter/complete_key_group_rewriter.cpp"
    RESULT_VARIABLE _count_result)
  if(NOT _count_result EQUAL 0)
    message(FATAL_ERROR "Cannot apply total DISTINCT count patch to the current NeuG source")
  endif()
  set(CICO_INTERPOSE_SOURCES "${_prepared};${_predicate_prepared};${_count_prepared}")
endif()
if(CICO_INTERPOSE_SOURCES)
  foreach(base incgq_rewrite_engine cico_exec)
    add_executable(${base}_patched ${base}.cc ${CICO_INTERPOSE_SOURCES})
    target_compile_options(${base}_patched PRIVATE -O3 -w -DANTLR4CPP_STATIC)
    target_include_directories(${base}_patched SYSTEM PRIVATE
      ${NEUG_SOURCE_DIR}/third_party/re2/include ${NEUG_SOURCE_DIR}/third_party/antlr4_cypher/include
      ${NEUG_SOURCE_DIR}/third_party/antlr4_runtime/src ${NEUG_SOURCE_DIR}/third_party/utf8proc/include)
    list(APPEND _targets ${base}_patched)
  endforeach()
endif()

foreach(target ${_targets})
# Must match the definitions NeuG headers were compiled with (see
# build/examples/cpp/CMakeFiles/check_index_rewrites.dir/flags.make).
target_compile_definitions(${target} PRIVATE
  BUILD_HTTP_SERVER CPPHTTPLIB_OPENSSL_SUPPORT ENABLE_CHRONO_YMD ENABLE_HTTP_PROTOCOL
  GFLAGS_IS_A_DLL=0 GLOG_CUSTOM_PREFIX_SUPPORT NEUG_CMAKE_VERSION="0.1.0" NEUG_VERSION="0.1.0"
  RAPIDJSON_HAS_CXX11=1 RAPIDJSON_HAS_CXX11_RANGE_FOR=1 RAPIDJSON_HAS_CXX11_RVALUE_REFS=1
  RAPIDJSON_HAS_STDSTRING=1 __64BIT__)
target_compile_options(${target} PRIVATE -fopenmp -march=native -Wall
  -Wno-psabi -Wno-class-memaccess -Wno-maybe-uninitialized -Wno-missing-requires
  -Wno-ignored-attributes -Wno-nonnull -Wno-deprecated -Wno-sign-compare
  -Wno-deprecated-declarations -Wno-attributes -Wno-array-bounds -Wno-restrict)
target_include_directories(${target} PRIVATE
  ${NEUG_BUILD_DIR}/include ${NEUG_SOURCE_DIR}/include
  ${NEUG_BUILD_DIR}/src/utils ${NEUG_BUILD_DIR}/src/compiler ${NEUG_SOURCE_DIR}/third_party
  ${NEUG_BUILD_DIR}/third_party/gflags/include ${NEUG_SOURCE_DIR}/third_party/glog/src
  ${NEUG_BUILD_DIR}/third_party/glog ${NEUG_SOURCE_DIR}/third_party/yaml-cpp/include
  ${NEUG_SOURCE_DIR}/third_party/protobuf/src ${NEUG_SOURCE_DIR}/third_party/abseil-cpp
  ${NEUG_SOURCE_DIR}/third_party/leveldb/include ${NEUG_BUILD_DIR}/third_party/brpc/output/include
  ${NEUG_BUILD_DIR}/_deps/arrow-src/cpp/src ${NEUG_BUILD_DIR}/_deps/arrow-build/src)
target_include_directories(${target} SYSTEM PRIVATE
  ${NEUG_SOURCE_DIR}/third_party/mimalloc/include ${NEUG_SOURCE_DIR}/third_party/fast_float/include
  ${NEUG_SOURCE_DIR}/third_party/HowardHinnant/date/include ${NEUG_SOURCE_DIR}/third_party/rapidjson/include
  ${NEUG_SOURCE_DIR}/third_party/flat_hash_map ${NEUG_SOURCE_DIR}/third_party/parallel-hashmap
  ${NEUG_SOURCE_DIR}/third_party/expected)

target_link_options(${target} PRIVATE -fopenmp -Wl,--export-dynamic -rdynamic
  "-Wl,-rpath,${NEUG_BUILD_DIR}/src:${NEUG_BUILD_DIR}/third_party/mimalloc")
target_link_libraries(${target} PRIVATE
  ${NEUG_BUILD_DIR}/src/libneug.so
  /usr/lib/x86_64-linux-gnu/libssl.so /usr/lib/x86_64-linux-gnu/libcrypto.so
  ${_deps}/release/libarrow_dataset.a ${_deps}/release/libarrow_acero.a ${_deps}/release/libarrow.a
  dl
  ${_deps}/snappy_ep/src/snappy_ep-install/lib/libsnappy.a
  ${_deps}/zlib_ep/src/zlib_ep-install/lib/libz.a
  ${_deps}/zstd_ep-install/lib/libzstd.a
  rt ${NEUG_BUILD_DIR}/third_party/glog/libglog.a /usr/lib/x86_64-linux-gnu/libunwind.so
  ${NEUG_BUILD_DIR}/third_party/gflags/libgflags_nothreads.a
  ${NEUG_BUILD_DIR}/third_party/mimalloc/libmimalloc.so.2.0 pthread)
endforeach()
