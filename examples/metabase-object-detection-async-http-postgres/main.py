import os
import sys
import typing
import pathlib
import argparse
import time
import shutil
from os import listdir
from os.path import isfile, join
from typing import Final, Any, List, Dict, Tuple

import cv2
import ffmpeg
import psycopg2
import requests
import numpy as np
from tqdm import tqdm
from tqdm.contrib import tzip
from google.cloud import storage

from utils import draw_detection


def download_data(bucket_name: str, blob_filename: str, dst_filename: str) -> bool:
    r""" Download a file from a GCS bucket into a local file

    Args:
        bucket_name (str): GCS bucket name
        blob_filename (str): file name to be downloaded from the in the GCS bucket
        dst_filename (str): the file name used to save the downloaded file

    Returns: bool
        a flag to indicate whether the downloading is successful

    """
    print(
        f"\n===== Download video {blob_filename} from GCS bucket {bucket_name} to {dst_filename} ..."
    )

    client = storage.Client.create_anonymous_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_filename)
    try:
        blob.download_to_filename(dst_filename)
    except Exception as e:
        print(e)
        os.remove(dst_filename)
        return False
    print("Done!")
    return True

def extract_frames_from_video(image_dir: str, filename: str, framerate: int=30) -> bool:
    r""" Extract frames from a video file at constant frames per second

    Args:
        image_dir (str): the directory where the extracted frames will be stored
        filename (str): name of the video file
        framerate (int): frames per second (fps) to extract the video. By default set to 30 fps.

    Returns: bool
        a flag to indicate whether the extraction is successful

    """
    if os.path.exists(image_dir) and os.path.isdir(image_dir):
        shutil.rmtree(image_dir)

    print(f"\n===== Extract frames from the video {filename} into {image_dir} ...")

    pathlib.Path(image_dir).mkdir(parents=True, exist_ok=True)
    try:
        (
            ffmpeg.input(filename)
                .filter('fps', fps=framerate)
                .output(join(image_dir, 'frame%5d.png'),
                        start_number=0)
                .run(capture_stdout=True, capture_stderr=True)
        )
        print("Done!\n")
        return True
    except ffmpeg.Error as error:
        print('stdout:', error.stdout.decode('utf8'))
        print('stderr:', error.stderr.decode('utf8'))
        shutil.rmtree(image_dir)
        return False


def generate_video_from_frames(image_dir: str, output_filename: str, framerate: int=30) -> bool:
    r""" Generate a video from a array of image frames

    Args:
        image_dir (str): the directory where the image frames are stored
        output_filename (str): the name of the video file to be generated
        framerate (int): fps of the video to be generated

    Returns: bool
        a flag to indicate whether the operation is successful

    """
    if os.path.exists(output_filename):
        os.remove(output_filename)

    print(
        f"\n=====Generate video {output_filename} from image files in {image_dir}..."
    )
    try:
        (
            ffmpeg
                .input(join(image_dir, '*.png'), pattern_type='glob', framerate=framerate)
                .output(output_filename, pix_fmt="yuv420p")
                .run()
        )
        print("Done!\n")
        return True
    except ffmpeg.Error as error:
        print(error)
        return False


def parse_detection_from_database(detection_ls: List[Dict[str, Any]]) -> Tuple[List[Tuple[float]], List[str], List[float]]:
    r""" Parse the raw detection output from the database

    Args:
        detection_ls: a list of detection outputs for standardised VDP Object Detection task
        [
            {
                "bounding_box": {
                    "left": 324,
                    "top": 102,
                    "width": 208,
                    "height": 405,
                },
                "category": "dog",
                "score": 0.9
            }
        ]

    Returns: parsed output, a tuple of
        List[Tuple[float]]: a list of detected bounding boxes in the format of (left, top, width, height)
        List[str]: a list of category labels, each of which corresponds to a detected bounding box. The length of this list must be the same as the detected bounding boxes.
        List[float]: a list of scores, each of which corresponds to a detected bounding box. The length of this list must be the same as the detected bounding boxes.

    """
    boxes_ltwh, categories, scores = [], [], []

    for det in detection_ls:
        boxes_ltwh.append((
            det["bounding_box"]["left"],
            det["bounding_box"]["top"],
            det["bounding_box"]["width"],
            det["bounding_box"]["height"]))
        categories.append(det["category"])
        scores.append(det["score"])

    return boxes_ltwh, categories, scores


if __name__ ==  '__main__':
    parser = argparse.ArgumentParser(description='Trigger VDP pipeline')
    parser.add_argument('--api-gateway-url', type=str,
                        default='http://localhost:8080', help='VDP API base URL')
    parser.add_argument("--pipeline-id", dest = 'pipeline_id', help =
                        "VDP pipeline ID", default = "detection", type = str)
    parser.add_argument("--pq-host", dest="pq_host", help = "PostgreSQL database host", type=str)
    parser.add_argument("--pq-port", dest="pq_port", help =
                        "PostgreSQL database port", default = 5432, type=int)
    parser.add_argument("--pq-database", dest="pq_database", help =
                        "PostgreSQL database name", default = "tutorial", type=str)
    parser.add_argument("--pq-username", dest="pq_username", help =
                        "PostgreSQL database username", default = "postgres", type=str)
    parser.add_argument("--pq-password", dest="pq_password", help =
                        "PostgreSQL database password", default = "password", type=str)
    parser.add_argument("--output-filename", dest = 'output_filename', help =
                        "Output video file name", default = "output.mp4", type = str)
    parser.add_argument("--framerate", dest = 'framerate', help =
                        "Frame rate of the video", default = 30, type = int)
    parser.add_argument("--skip-draw", dest="draw", action="store_false", help =
                        "Skip draw detections on images")

    opt = parser.parse_args()

    ###############################################################################
    # Download video
    ###############################################################################

    video_filename = join(os.path.dirname(os.path.realpath(__file__)), "cows_dornick.mp4")

    if skip_download := os.path.exists(video_filename):
        print("\n===== Skip downloading video")
    else:
        success = download_data(bucket_name='public-europe-west2-c-artifacts',
        blob_filename="vdp/tutorial/cows_dornick/cows_dornick.mp4",
        dst_filename=video_filename)
        if not success:
            sys.exit(1)

    ###############################################################################
    # Extract frames from the video file
    ###############################################################################

    image_dir = join(os.path.dirname(os.path.realpath(__file__)), "inputs")

    skip_extract = False
    if os.path.exists(image_dir) and os.path.isdir(image_dir):
        if os.listdir(image_dir):
            skip_extract = True
    if skip_extract:
        print(f"\n===== Skip extracting frames from video {video_filename}")
    else:
        success = extract_frames_from_video(image_dir, video_filename, framerate=opt.framerate)
        if not success:
            sys.exit(1)


    ###############################################################################
    # Trigger pipeline to process video frames
    ###############################################################################

    batch_size = 1
    img_files = [filename for filename in sorted(listdir(image_dir)) if isfile(
        join(image_dir, filename)) and not filename.startswith(".")]
    img_batch = [img_files[i:i+batch_size]  for i in range(0, len(img_files), batch_size)]
    filenames = [file for files in img_batch for file in files]
    data_mapping_indices = []

    print(
        f"\n=====Trigger {opt.pipeline_id} pipeline to process images in '{image_dir}'\n"
    )
    for files in tqdm(img_batch):
        resp = requests.post(f'{opt.api_gateway_url}/v1alpha/pipelines/{opt.pipeline_id}/triggerAsyncMultipart',
                        files=[("file", (filename, open(join(image_dir, filename), 'rb'))) for filename in files])
        if resp.status_code == 200:
            data_mapping_indices += resp.json()['data_mapping_indices']
        else:
            sys.exit(1)

    # ###############################################################################
    # # Draw detections on video frames
    # ###############################################################################

    if opt.draw:
        time.sleep(10)
        conn = None
        print("#", end="", flush=True)
        assert len(filenames) == len(
            data_mapping_indices
        ), f"number of files {len(filenames)} not consistent with number of records {len(data_mapping_indices)}"

        # Create output directory
        output_dir = join(os.path.dirname(os.path.realpath(__file__)), "outputs")
        pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)

        for filename, mapping_index in tzip(filenames, data_mapping_indices):
            # Fetch detections from destination PostgreSQL database
            try:
                conn = psycopg2.connect(
                    user=opt.pq_username, password=opt.pq_password, host=opt.pq_host, port=opt.pq_port, database=opt.pq_database)
                cur = conn.cursor()
                cur.execute(
                    f"""SELECT _airbyte_raw_vdp._airbyte_data->'detection'->'objects' AS "objects" from _airbyte_raw_vdp WHERE _airbyte_raw_vdp._airbyte_data->>'index' = '{mapping_index}'"""
                )
                row = cur.fetchone()[0]

                boxes_ltwh, categories, scores = parse_detection_from_database(row)
                buffer = open(join(image_dir, filename), 'rb')
                arr = np.asarray(bytearray(buffer.read()), dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                img_draw = draw_detection(img, boxes_ltwh, categories, scores)
                cv2.imwrite(join(os.path.dirname(os.path.realpath(__file__)), output_dir, filename), img_draw)
                cur.close()

            except (Exception, psycopg2.DatabaseError) as error:
                print(error)

        if conn is not None:
            conn.close()

        # Generate video with detections
        success = generate_video_from_frames(output_dir, opt.output_filename, framerate=opt.framerate)
