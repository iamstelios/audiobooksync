import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from audiobooksync.onnx_ctc import _stage_openvino_model, load_session


class ProviderSelectionTests(unittest.TestCase):
    def setUp(self):
        class OrtFailure(Exception):
            pass

        class SessionOptions:
            def __init__(self):
                self.log_severity_level = None
                self.devices = None
                self.provider_options = None

            def add_provider_for_devices(self, devices, options):
                self.devices = devices
                self.provider_options = options

        class Session:
            def get_providers(self):
                return ["CPUExecutionProvider"]

        self.options = SessionOptions()
        self.session = Session()
        state = types.SimpleNamespace(
            Fail=OrtFailure,
            RuntimeException=OrtFailure,
            InvalidArgument=OrtFailure,
            NotImplemented=OrtFailure,
        )
        self.ort = types.SimpleNamespace(
            SessionOptions=lambda: self.options,
            InferenceSession=Mock(return_value=self.session),
            register_execution_provider_library=Mock(),
            get_ep_devices=Mock(),
            capi=types.SimpleNamespace(onnxruntime_pybind11_state=state),
        )
        self.openvino = types.ModuleType("onnxruntime_ep_openvino")
        self.openvino.get_library_path = Mock(return_value="/fake/openvino-plugin.so")
        self.openvino.get_ep_name = Mock(return_value="OpenVINOExecutionProvider")

    def test_selects_openvino_gpu_when_available(self):
        gpu = types.SimpleNamespace(
            ep_name="OpenVINOExecutionProvider",
            ep_metadata={"ov_device": "GPU"},
        )
        self.ort.get_ep_devices.return_value = [gpu]
        messages = []

        with patch.dict(
            sys.modules,
            {"onnxruntime": self.ort, "onnxruntime_ep_openvino": self.openvino},
        ):
            session, providers = load_session("model.onnx", progress=messages.append)

        self.assertIs(session, self.session)
        self.assertEqual(providers, ["OpenVINOExecutionProvider (GPU)"])
        self.assertEqual(self.options.devices, [gpu])
        self.assertEqual(messages, [])
        self.ort.register_execution_provider_library.assert_called_once_with(
            "openvino_ep", "/fake/openvino-plugin.so"
        )

    def test_falls_back_to_cuda_and_cpu_without_openvino_gpu(self):
        cpu = types.SimpleNamespace(
            ep_name="OpenVINOExecutionProvider",
            ep_metadata={"ov_device": "CPU"},
        )
        self.ort.get_ep_devices.return_value = [cpu]
        messages = []

        with patch.dict(
            sys.modules,
            {"onnxruntime": self.ort, "onnxruntime_ep_openvino": self.openvino},
        ):
            session, providers = load_session("model.onnx", progress=messages.append)

        self.assertIs(session, self.session)
        self.assertEqual(providers, ["CPUExecutionProvider"])
        self.ort.InferenceSession.assert_called_once_with(
            "model.onnx",
            self.options,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        self.assertTrue(any("trying CUDA/CPU" in message for message in messages))

    def test_explicit_provider_list_skips_openvino(self):
        with patch.dict(sys.modules, {"onnxruntime": self.ort}):
            session, providers = load_session("model.onnx", ["CPUExecutionProvider"])

        self.assertIs(session, self.session)
        self.assertEqual(providers, ["CPUExecutionProvider"])
        self.ort.register_execution_provider_library.assert_not_called()


class OpenVinoModelStagingTests(unittest.TestCase):
    def test_external_data_is_staged_beside_the_model(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            blob_directory = root / "blobs"
            snapshot_directory = root / "snapshot" / "onnx"
            cache_directory = root / "cache"
            blob_directory.mkdir()
            snapshot_directory.mkdir(parents=True)

            model_blob = blob_directory / "model.onnx"
            data_blob = blob_directory / "model.onnx_data"
            model_blob.write_bytes(b"onnx")
            data_blob.write_bytes(b"external-data")
            model_path = snapshot_directory / "model.onnx"
            data_path = snapshot_directory / "model.onnx_data"
            model_path.symlink_to(model_blob)
            data_path.symlink_to(data_blob)

            with patch.dict(os.environ, {"OPENVINO_CACHE_DIR": str(cache_directory)}):
                staged_model = _stage_openvino_model(model_path)

            staged_data = Path(f"{staged_model}_data")
            self.assertTrue(staged_model.is_file())
            self.assertTrue(staged_data.is_file())
            self.assertEqual(staged_model.parent, staged_data.parent)
            self.assertEqual(staged_model.read_bytes(), model_blob.read_bytes())
            self.assertEqual(staged_data.read_bytes(), data_blob.read_bytes())


if __name__ == "__main__":
    unittest.main()
