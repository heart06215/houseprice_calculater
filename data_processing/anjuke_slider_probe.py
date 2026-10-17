"""One bounded cloud experiment against the observed image-slider UI."""

import math
from urllib.parse import urlsplit


def locate_piece(background, foreground, y_hint=None):
    import cv2
    import numpy as np

    bg = cv2.imdecode(np.frombuffer(background, np.uint8), cv2.IMREAD_COLOR)
    fg = cv2.imdecode(np.frombuffer(foreground, np.uint8), cv2.IMREAD_UNCHANGED)
    if bg is None or fg is None or fg.ndim != 3 or fg.shape[2] != 4:
        raise ValueError('Expected a background and an RGBA puzzle piece')
    alpha = (fg[:, :, 3] > 32).astype(np.uint8) * 255
    x, y, width, height = cv2.boundingRect(alpha)
    if width < 10 or height < 10 or width >= bg.shape[1] or height >= bg.shape[0]:
        raise ValueError('Unsupported puzzle dimensions')
    edges = cv2.Canny(cv2.cvtColor(fg[:, :, :3], cv2.COLOR_BGR2GRAY), 60, 180)
    edges = cv2.bitwise_or(edges, cv2.Canny(alpha, 60, 180))[y:y+height, x:x+width]
    bg_edges = cv2.Canny(cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY), 60, 180)
    scores = cv2.matchTemplate(bg_edges, edges, cv2.TM_CCOEFF_NORMED)
    scores[:, :max(12, width // 2)] = -1
    if y_hint is not None:
        expected_y = round(y_hint + y)
        low, high = max(0, expected_y - 4), min(scores.shape[0], expected_y + 5)
        if low >= high:
            raise ValueError('Puzzle vertical position outside background')
        scores[:low, :] = -1
        scores[high:, :] = -1
    _, confidence, _, location = cv2.minMaxLoc(scores)
    if not math.isfinite(confidence) or confidence < 0.12:
        raise ValueError('No sufficiently distinct puzzle match')
    return dict(target_x=location[0], target_y=location[1], alpha_x=x, alpha_y=y,
                image_width=bg.shape[1], image_height=bg.shape[0], confidence=confidence)


def next_pointer(current, error, limit):
    """Bounded visual feedback handles startup offsets and delayed UI movement."""
    return min(limit, max(0, current + max(-8, min(8, error * 0.65))))


def try_slider(page, context, out, key):
    """Use the site's own SDK for all challenge state and verification requests."""
    bg_image = page.locator('img.yidun_bg-img:visible').first
    piece = page.locator('img.yidun_jigsaw:visible').first
    slider = page.locator('.yidun_slider:visible').first
    control = page.locator('.yidun_control:visible').first
    if not all(element.count() for element in (bg_image, piece, slider, control)):
        return dict(state='not_ready')
    boxes = [element.bounding_box() for element in (bg_image, piece, slider, control)]
    if not all(boxes):
        return dict(state='not_ready')
    bg_box, piece_box, slider_box, control_box = boxes
    images = []
    for element in (bg_image, piece):
        url = element.get_attribute('src') or ''
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'necaptcha.nosdn.127.net':
            return dict(state='unsupported_image_origin')
        response = context.request.get(url, timeout=10000)
        body = response.body()
        if not response.ok or len(body) > 2000000:
            return dict(state='image_fetch_failed')
        images.append(body)
    from pathlib import Path
    folder = Path(out) / 'puzzles'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{key}-background.jpg').write_bytes(images[0])
    (folder / f'{key}-foreground.png').write_bytes(images[1])
    # Read native dimensions once; the CSS/native ratio may differ from 1.
    import cv2
    import numpy as np
    bg_native = cv2.imdecode(np.frombuffer(images[0], np.uint8), cv2.IMREAD_COLOR)
    scale = bg_box['width'] / bg_native.shape[1]
    y_hint = (piece_box['y'] - bg_box['y']) / scale
    match = locate_piece(*images, y_hint=y_hint)
    displacement = (match['target_x'] - match['alpha_x']) * scale - (piece_box['x'] - bg_box['x'])
    if not 12 < displacement < bg_box['width'] - 10:
        return dict(state='match_outside_drag_range', **match)
    start_x = slider_box['x'] + slider_box['width'] / 2
    start_y = slider_box['y'] + slider_box['height'] / 2
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    try:
        distance = 0
        limit = control_box['width'] - slider_box['width']
        samples = []
        aligned = False
        for step in range(70):
            box = piece.bounding_box()
            if not box:
                break
            visible = box['x'] - piece_box['x']
            error = displacement - visible
            samples.append(dict(pointer=round(distance, 2), piece=round(visible, 2), error=round(error, 2)))
            if abs(error) <= 0.8:
                page.wait_for_timeout(350)
                settled = piece.bounding_box()
                if settled and abs(displacement - (settled['x'] - piece_box['x'])) <= 1:
                    aligned = True
                    break
            distance = next_pointer(distance, error, limit)
            page.mouse.move(start_x + distance, start_y + math.sin(step / 10) * 0.7)
            page.wait_for_timeout(55)
        return dict(state='drag_submitted' if aligned else 'drag_not_aligned',
                    distance=distance, target_displacement=displacement,
                    aligned=aligned, motion=samples, **match)
    finally:
        page.mouse.up()
