import os
import cv2
import shutil
import random
import numpy as np
from tqdm import tqdm
import fitz  # PyMuPDF - vector PDF render (no quality loss / aspect preserved)

try:
    import torch
    import torch.nn.functional as F

    TORCH_AVAILABLE = True
except ImportError:
    torch = None
    F = None
    TORCH_AVAILABLE = False

USE_GPU = False
DEVICE = "cpu"


def set_device(use_gpu: bool = True) -> str:
    """Enable/disable CUDA for augmentations. Returns active device string."""
    global USE_GPU, DEVICE
    if use_gpu and TORCH_AVAILABLE and torch.cuda.is_available():
        USE_GPU = True
        DEVICE = "cuda"
        torch.backends.cudnn.benchmark = True
    else:
        USE_GPU = False
        DEVICE = "cpu"
    return DEVICE


def gpu_info() -> dict:
    available = bool(TORCH_AVAILABLE and torch.cuda.is_available())
    name = torch.cuda.get_device_name(0) if available else None
    return {
        "torch": TORCH_AVAILABLE,
        "cuda": available,
        "name": name,
        "device": DEVICE,
        "use_gpu": USE_GPU,
    }


def _to_tensor(img: np.ndarray):
    """BGR uint8 HWC -> float tensor NCHW on DEVICE (0-255)."""
    t = torch.from_numpy(img).permute(2, 0, 1).unsqueeze(0).float()
    return t.to(DEVICE, non_blocking=True)


def _to_numpy(t) -> np.ndarray:
    """float NCHW -> BGR uint8 HWC."""
    t = t.squeeze(0).clamp(0, 255).byte().permute(1, 2, 0).contiguous()
    return t.cpu().numpy()

# =====================================================
# PATHS
# =====================================================

# Existing labeled image set (optional; set RUN_LABELED_IMAGES=True to augment)
IMAGE_DIR = r"C:\Users\AIMLE\Desktop\New folder\images"
LABEL_DIR = r"C:\Users\AIMLE\Desktop\New folder\labels"
OUT_IMAGE_DIR = r"C:\Users\AIMLE\Desktop\New folder\aumented data\images"
OUT_LABEL_DIR = r"C:\Users\AIMLE\Desktop\New folder\aumented data\labels"

# Vector PDF - rendered page-wise with PyMuPDF, then augmented
PDF_PATH = r"C:\Users\AIMLE\Downloads\DRD180828_109300003660_16_04_2026_032429_A2.pdf"
PDF_RENDER_DPI = 150  # high enough for vector CAD detail without huge rasters
PDF_PAGE_DIR = r"C:\Users\AIMLE\Downloads\DRD180828_pages\images"
PDF_AUG_IMAGE_DIR = r"C:\Users\AIMLE\Downloads\DRD180828_pages\augmented\images"
PDF_AUG_LABEL_DIR = r"C:\Users\AIMLE\Downloads\DRD180828_pages\augmented\labels"

# What to run
RUN_VECTOR_PDF = True
RUN_LABELED_IMAGES = False

# =====================================================
# VECTOR PDF -> IMAGES (PyMuPDF)
# =====================================================

def render_vector_pdf(pdf_path: str, out_dir: str, dpi: int = 300) -> list:
    """
    Render each PDF page to PNG at the given DPI.
    Aspect ratio is preserved (page rect * zoom). Vector text/lines stay sharp.
    Returns list of written image paths.
    """
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    os.makedirs(out_dir, exist_ok=True)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    written = []

    doc = fitz.open(pdf_path)
    try:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out_name = f"{stem}_page_{i + 1:03d}.png"
            out_path = os.path.join(out_dir, out_name)
            pix.save(out_path)
            written.append(out_path)
            print(f"[pdf] page {i + 1}/{len(doc)} -> {out_name} ({pix.width}x{pix.height})")
    finally:
        doc.close()

    print(f"[pdf] rendered {len(written)} page(s) at {dpi} DPI -> {out_dir}")
    return written


# =====================================================
# AUGMENTATIONS (CPU + optional CUDA via PyTorch)
# =====================================================

def _gauss_kernel1d(k: int, sigma: float, device):
    x = torch.arange(k, device=device, dtype=torch.float32) - (k - 1) / 2
    k1d = torch.exp(-(x ** 2) / (2 * sigma ** 2))
    k1d = k1d / k1d.sum()
    return k1d


def _gaussian_blur_torch(t, k: int):
    sigma = 0.3 * ((k - 1) * 0.5 - 1) + 0.8
    k1d = _gauss_kernel1d(k, sigma, t.device)
    c = t.shape[1]
    kw = k1d.view(1, 1, 1, k).repeat(c, 1, 1, 1)
    kh = k1d.view(1, 1, k, 1).repeat(c, 1, 1, 1)
    pad = k // 2
    t = F.conv2d(t, kw, padding=(0, pad), groups=c)
    t = F.conv2d(t, kh, padding=(pad, 0), groups=c)
    return t


def gaussian_noise(img):
    sigma = random.uniform(5, 15)
    if USE_GPU:
        t = _to_tensor(img)
        noise = torch.randn_like(t) * sigma
        return _to_numpy(t + noise)
    noise = np.random.normal(0, sigma, img.shape).astype(np.float32)
    out = img.astype(np.float32) + noise
    return np.clip(out, 0, 255).astype(np.uint8)


def jpeg_compression(img):
    # JPEG encode/decode stays on CPU (OpenCV)
    quality = random.randint(35, 70)
    _, enc = cv2.imencode(".jpg", img,
                          [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return cv2.imdecode(enc, 1)


def gaussian_blur(img):
    k = random.choice([3, 5])
    if USE_GPU:
        return _to_numpy(_gaussian_blur_torch(_to_tensor(img), k))
    return cv2.GaussianBlur(img, (k, k), 0)


def brightness(img):
    beta = random.randint(-25, 25)
    if USE_GPU:
        return _to_numpy(_to_tensor(img) + beta)
    return cv2.convertScaleAbs(img, alpha=1.0, beta=beta)


def contrast(img):
    alpha = random.uniform(0.8, 1.3)
    if USE_GPU:
        return _to_numpy(_to_tensor(img) * alpha)
    return cv2.convertScaleAbs(img, alpha=alpha, beta=0)


def gamma(img):
    g = random.uniform(0.8, 1.4)
    if USE_GPU:
        t = _to_tensor(img) / 255.0
        return _to_numpy((t ** (1.0 / g)) * 255.0)
    inv = 1.0 / g
    table = np.array(
        [((i / 255.0) ** inv) * 255 for i in np.arange(256)]
    ).astype("uint8")
    return cv2.LUT(img, table)


def clahe(img):
    # CLAHE stays on CPU (OpenCV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cla = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = cla.apply(gray)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def dpi_scaling(img):
    h, w = img.shape[:2]
    scale = random.uniform(0.7, 1.3)
    if USE_GPU:
        t = _to_tensor(img)
        nh, nw = max(1, int(h * scale)), max(1, int(w * scale))
        t = F.interpolate(t, size=(nh, nw), mode="bilinear", align_corners=False)
        t = F.interpolate(t, size=(h, w), mode="bilinear", align_corners=False)
        return _to_numpy(t)
    resized = cv2.resize(
        img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LINEAR
    )
    return cv2.resize(resized, (w, h), interpolation=cv2.INTER_LINEAR)


def line_thickness(img):
    if USE_GPU:
        t = _to_tensor(img)
        # Approximate BGR->gray luminance
        gray = (0.114 * t[:, 0:1] + 0.587 * t[:, 1:2] + 0.299 * t[:, 2:3])
        # Max/min pool ~ dilate/erode
        if random.random() < 0.5:
            gray = F.max_pool2d(gray, 2, stride=1, padding=1)
            gray = gray[:, :, : img.shape[0], : img.shape[1]]
        else:
            gray = -F.max_pool2d(-gray, 2, stride=1, padding=1)
            gray = gray[:, :, : img.shape[0], : img.shape[1]]
        out = gray.repeat(1, 3, 1, 1)
        return _to_numpy(out)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    kernel = np.ones((2, 2), np.uint8)
    if random.random() < 0.5:
        gray = cv2.dilate(gray, kernel, iterations=1)
    else:
        gray = cv2.erode(gray, kernel, iterations=1)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def black_enhancement(img):
    amount = random.randint(20, 50)
    if USE_GPU:
        t = _to_tensor(img)
        gray = 0.114 * t[:, 0:1] + 0.587 * t[:, 1:2] + 0.299 * t[:, 2:3]
        mask = gray < 150
        dark = torch.where(mask, (gray - amount).clamp(0, 255), gray)
        return _to_numpy(dark.repeat(1, 3, 1, 1))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)
    dark = gray.copy()
    dark[mask > 0] = np.clip(dark[mask > 0] - amount, 0, 255)
    return cv2.cvtColor(dark, cv2.COLOR_GRAY2BGR)


AUGS = [
    ("noise", gaussian_noise),
    ("jpeg", jpeg_compression),
    ("blur", gaussian_blur),
    ("brightness", brightness),
    ("contrast", contrast),
    ("gamma", gamma),
    ("clahe", clahe),
    ("dpi", dpi_scaling),
    ("line", line_thickness),
    ("black", black_enhancement),
]


# =====================================================
# PROCESS IMAGE FOLDER
# =====================================================

def augment_folder(image_dir, label_dir, out_image_dir, out_label_dir, use_gpu: bool = False):
    device = set_device(use_gpu)
    print(f"[aug] device={device}" + (f" ({gpu_info().get('name')})" if device == "cuda" else ""))

    os.makedirs(out_image_dir, exist_ok=True)
    os.makedirs(out_label_dir, exist_ok=True)

    images = [
        f for f in os.listdir(image_dir)
        if f.lower().endswith(
            (".jpg", ".jpeg", ".png", ".bmp", ".tif")
        )
    ]

    for img_name in tqdm(images, desc=os.path.basename(image_dir) or "images"):

        img_path = os.path.join(image_dir, img_name)

        label_name = os.path.splitext(img_name)[0] + ".txt"

        label_path = os.path.join(label_dir, label_name) if label_dir else ""

        img = cv2.imread(img_path)
        if img is None:
            print(f"[skip] could not read {img_path}")
            continue

        # Copy Original
        cv2.imwrite(
            os.path.join(out_image_dir, img_name),
            img
        )

        if label_path and os.path.exists(label_path):
            shutil.copy(
                label_path,
                os.path.join(out_label_dir, label_name)
            )

        # Generate one image for each augmentation
        for aug_name, aug in AUGS:

            aug_img = aug(img.copy())

            new_img = (
                os.path.splitext(img_name)[0]
                + "_"
                + aug_name
                + ".jpg"
            )

            new_label = (
                os.path.splitext(img_name)[0]
                + "_"
                + aug_name
                + ".txt"
            )

            cv2.imwrite(
                os.path.join(out_image_dir, new_img),
                aug_img,
            )

            if label_path and os.path.exists(label_path):
                shutil.copy(
                    label_path,
                    os.path.join(out_label_dir, new_label),
                )

        if USE_GPU:
            torch.cuda.empty_cache()


# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":
    set_device(True)
    info = gpu_info()
    print(f"[gpu] cuda={info['cuda']} device={info['device']} name={info['name']}")

    if RUN_VECTOR_PDF:
        print("=== Vector PDF (PyMuPDF) ===")
        render_vector_pdf(PDF_PATH, PDF_PAGE_DIR, dpi=PDF_RENDER_DPI)
        print("=== Augment PDF pages ===")
        augment_folder(
            PDF_PAGE_DIR,
            label_dir="",
            out_image_dir=PDF_AUG_IMAGE_DIR,
            out_label_dir=PDF_AUG_LABEL_DIR,
            use_gpu=True,
        )

    if RUN_LABELED_IMAGES:
        print("=== Augment labeled images ===")
        augment_folder(
            IMAGE_DIR, LABEL_DIR, OUT_IMAGE_DIR, OUT_LABEL_DIR, use_gpu=True
        )

    print("\nFinished.")
