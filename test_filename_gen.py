"""
Quick test for generate_media_filename() without real cameras.

Run with:
    cd /home/pi/CamUI-WebRTC && python test_filename_gen.py
"""
import os
import tempfile

# ── Minimal stubs ────────────────────────────────────────────────────────────

class FakeCamera:
    def __init__(self, num, name):
        self.camera_num = num
        self.name = name

class FakeManager:
    def __init__(self, cameras: dict, upload_folder: str):
        self.cameras = cameras          # {num: FakeCamera}
        self.media_upload_folder = upload_folder

    def get_camera(self, cam_num):
        return self.cameras.get(cam_num)

    # Copy of the real implementation
    def generate_media_filename(self, cam_num, extension, timestamp):
        if not extension.startswith("."):
            extension = "." + extension

        multi_cam = len(self.cameras) > 1 and cam_num
        if multi_cam:
            cam = self.get_camera(cam_num)
            cam_suffix = (cam.name if cam else f"Cam{cam_num}").lower().replace(" ", "_")
        else:
            cam_suffix = None

        existing = set(os.listdir(self.media_upload_folder))
        counter = 0
        while True:
            counter_part = f"_{counter}" if counter else ""
            name = (
                f"{timestamp}{counter_part}_{cam_suffix}{extension}"
                if cam_suffix else
                f"{timestamp}{counter_part}{extension}"
            )
            if name not in existing:
                return name
            counter += 1

# ── Tests ────────────────────────────────────────────────────────────────────

def run_tests():
    ts = "2026-04-18_12-00-00"

    with tempfile.TemporaryDirectory() as folder:
        mgr_single = FakeManager({1: FakeCamera(1, "Cam1")}, folder)
        mgr_multi  = FakeManager(
            {1: FakeCamera(1, "Front"), 2: FakeCamera(2, "Back")},
            folder
        )

        results = []

        def check(label, got, expected):
            ok = got == expected
            results.append((ok, label, got, expected))

        # Single camera: no suffix
        f = mgr_single.generate_media_filename(1, ".jpg", ts)
        check("single-cam photo", f, f"{ts}.jpg")

        # Multi-camera: camera name suffix
        f = mgr_multi.generate_media_filename(1, ".jpg", ts)
        check("multi-cam cam1 photo", f, f"{ts}_front.jpg")

        f = mgr_multi.generate_media_filename(2, ".mp4", ts)
        check("multi-cam cam2 video", f, f"{ts}_back.mp4")

        # Different extension, same timestamp → no counter needed
        open(os.path.join(folder, f"{ts}_front.jpg"), "w").close()
        f = mgr_multi.generate_media_filename(1, ".mp4", ts)
        check("same timestamp, different ext → no counter", f, f"{ts}_front.mp4")

        # Same extension → counter between timestamp and name
        open(os.path.join(folder, f"{ts}_front.mp4"), "w").close()
        f = mgr_multi.generate_media_filename(1, ".mp4", ts)
        check("collision → counter before cam name", f, f"{ts}_1_front.mp4")

        # Single-cam collision
        open(os.path.join(folder, f"{ts}.jpg"), "w").close()
        f = mgr_single.generate_media_filename(1, ".jpg", ts)
        check("single-cam collision → _1 suffix", f, f"{ts}_1.jpg")

        # Double collision
        open(os.path.join(folder, f"{ts}_1_front.mp4"), "w").close()
        f = mgr_multi.generate_media_filename(1, ".mp4", ts)
        check("double collision → _2", f, f"{ts}_2_front.mp4")

    # Print results
    passed = sum(1 for ok, *_ in results if ok)
    print(f"\nResults: {passed}/{len(results)} passed\n")
    for ok, label, got, expected in results:
        status = "✓" if ok else "✗"
        print(f"  {status} {label}")
        if not ok:
            print(f"      expected: {expected}")
            print(f"      got:      {got}")
    print()

if __name__ == "__main__":
    run_tests()
