#!/bin/bash

set -xe

arch=$1

source $(dirname "$0")/tc-tests-utils.sh

mkdir -p ${TASKCLUSTER_ARTIFACTS} || true

cp ${DS_ROOT_TASK}/DeepSpeech/ds/tensorflow/bazel*.log ${TASKCLUSTER_ARTIFACTS}/

package_native_client "native_client.tar.xz"

package_libdeepspeech_as_zip "libmozilla_voice_stt.zip"

case $arch in
"--x86_64")
  release_folder="Release-iphonesimulator"
  artifact_name="mozilla_voice_stt.framework.x86_64.tar.xz"
  ;;
"--arm64")
  release_folder="Release-iphoneos"
  artifact_name="mozilla_voice_stt.framework.arm64.tar.xz"
;;
esac

${TAR} -cf - \
       -C ${DS_ROOT_TASK}/DeepSpeech/ds/native_client/swift/DerivedData/Build/Products/${release_folder}/ mozilla_voice_stt.framework \
       | ${XZ} > "${TASKCLUSTER_ARTIFACTS}/${artifact_name}"