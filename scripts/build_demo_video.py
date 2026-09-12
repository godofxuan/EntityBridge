"""Build a five-minute captioned walkthrough from verified browser screenshots.

Requires an independently supplied FFmpeg with libass/libx264; no application dependency.
This is a timed screenshot walkthrough, not an uncut real-time screen recording.
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

CHAPTERS = [
    ("09-real-data.png", 45, "真实双源：企业名称可能改变。登记号只供有标识入口与评估端使用。"),
    ("10-measurements.png", 55, "新封存测试 7,170 家企业：精确名称 F1=0.9952，Splink=0.9399。没有掩盖无增益结果。"),
    ("01-directory.png", 20, "切换到合成治理样例。初始 5 条来源记录被归入 3 个企业，存在一个错误桥接。"),
    ("02-review.png", 30, "逐对查看名称、地址和评分依据。合成评分只用于演示治理，不进入真实算法统计。"),
    ("04-revoke-preview.png", 35, "判定不同已将企业数改为 4。撤销旧判断前展示具体归属；撤销不必然拆分或合并。"),
    ("05-prepared.png", 20, "撤销先生成候选版本。此时查询仍读取原发布版本，避免暴露半成品。"),
    ("03-history.png", 15, "校验并发布后切换指针。建立、撤销与过期均为追加事件，历史判断不会消失。"),
    ("11-incremental.png", 50, "固定模型下更名、删除、插入均逐边对照全量。旧新候选及受抑制边共同决定影响闭包。"),
    ("06-old-id.png", 15, "旧企业 ID 返回拆分后的全部当前去向，不随机重定向到一个企业。"),
    ("07-historical.png", 15, "指定历史版本仍能查询原来的 3 个成员及出处，导出也绑定固定版本。"),
]


def timestamp(seconds):
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02},000"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    demo = root / "docs/demo"
    frames = root / ".tools/media/video-frames"
    frames.mkdir(parents=True, exist_ok=True)
    elapsed, concat, subtitles = 0, [], []
    hashes = {}
    for index, (name, duration, caption) in enumerate(CHAPTERS, 1):
        source = demo / name
        hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
        # Uniform decoded dimensions/pixel formats prevent FFmpeg from resetting
        # the fps/subtitle filter on every differently sized full-page capture.
        subprocess.run([str(args.ffmpeg.resolve()), "-y", "-loglevel", "error", "-i", str(source),
            "-vf", "scale=1600:880:force_original_aspect_ratio=decrease,setsar=1,pad=1600:1000:(ow-iw)/2:0:color=0x142c26",
            "-pix_fmt", "rgb24", "-frames:v", "1", str(frames / name)], check=True, timeout=30)
        concat.extend([f"file '../../.tools/media/video-frames/{name}'", f"duration {duration}"])
        subtitles.append(f"{index}\n{timestamp(elapsed)} --> {timestamp(elapsed + duration)}\n{caption}\n")
        elapsed += duration
    assert elapsed == 300
    concat.append(f"file '../../.tools/media/video-frames/{CHAPTERS[-1][0]}'")
    (demo / "frames.txt").write_text("\n".join(concat) + "\n", encoding="utf-8")
    (demo / "captions.srt").write_text("\n".join(subtitles), encoding="utf-8")
    output = demo / "EntityBridge-5min.mp4"
    filters = ("fps=2,subtitles=captions.srt:force_style='FontName=Microsoft YaHei,FontSize=12,"
               "PrimaryColour=&H00FFFFFF,Outline=1,Shadow=0,MarginV=10'")
    with (demo / "video_build.log").open("w", encoding="utf-8") as log:
        subprocess.run([str(args.ffmpeg.resolve()), "-y", "-f", "concat", "-safe", "0", "-i", "frames.txt",
            "-vf", filters, "-t", "300", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", output.name], cwd=demo,
            stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    verification = subprocess.run([str(args.ffmpeg.resolve()), "-v", "error", "-i", str(output),
        "-map", "0:v:0", "-c", "copy", "-f", "null", "-", "-progress", "pipe:1", "-nostats"],
        capture_output=True, text=True, check=True, timeout=30)
    measured = dict(line.split("=", 1) for line in verification.stdout.splitlines() if "=" in line)
    assert int(measured["frame"]) == 600, measured
    (demo / "video_manifest.json").write_text(json.dumps({
        "kind": "captioned verified screenshot walkthrough; no audio; not an uncut live recording",
        "duration_seconds": 300, "verified_frames": int(measured["frame"]), "frames_per_second": 2, "chapters": CHAPTERS,
        "screenshots_sha256": hashes, "video_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "bytes": output.stat().st_size}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
