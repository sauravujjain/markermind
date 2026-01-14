#!/usr/bin/env python3
"""
DIAGNOSTIC: Verify Raster NFP Implementation

This script visualizes each step to confirm:
1. Boundary computation (dilation XOR original)
2. Contact map via FFT convolution
3. Score map computation

Run this BEFORE the full experiment to validate the algorithm.
"""

import numpy as np
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw
from pathlib import Path

# Use numpy fallbacks to avoid scipy issues
def binary_dilation_numpy(arr, iterations=1):
    """Simple binary dilation using numpy."""
    from numpy.lib.stride_tricks import sliding_window_view

    kernel = np.array([[0, 1, 0],
                       [1, 1, 1],
                       [0, 1, 0]], dtype=np.float32)

    result = arr.astype(np.float32).copy()
    for _ in range(iterations):
        padded = np.pad(result, 1, mode='constant', constant_values=0)
        windows = sliding_window_view(padded, (3, 3))
        dilated = np.any(windows * kernel > 0, axis=(2, 3)).astype(np.float32)
        result = dilated

    return result


def cpu_fftconvolve(a, b, mode='full'):
    """FFT-based convolution using pure numpy."""
    s1 = np.array(a.shape)
    s2 = np.array(b.shape)
    shape = s1 + s2 - 1

    fft_a = np.fft.fft2(a, shape)
    fft_b = np.fft.fft2(b, shape)
    result = np.fft.ifft2(fft_a * fft_b).real

    if mode == 'valid':
        start_0 = s2[0] - 1
        start_1 = s2[1] - 1
        end_0 = start_0 + s1[0] - s2[0] + 1
        end_1 = start_1 + s1[1] - s2[1] + 1
        return result[start_0:end_0, start_1:end_1]
    return result


OUTPUT_DIR = Path("experiment_results/nfp_diagnostic")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# Step 1: Create a simple test piece
# =============================================================================

def create_test_piece():
    """Create a simple L-shaped piece for testing."""
    # L-shape vertices (in pixels)
    img = Image.new('L', (50, 60), 0)
    draw = ImageDraw.Draw(img)

    # Draw an L-shape
    draw.polygon([
        (5, 5), (25, 5), (25, 35), (45, 35), (45, 55), (5, 55)
    ], fill=1)

    raster = np.array(img, dtype=np.float32)
    return raster

def create_test_container_with_pieces():
    """Create a container with some pieces already placed."""
    container = np.zeros((100, 200), dtype=np.float32)

    # Place a rectangle on the left
    container[10:50, 10:40] = 1.0

    # Place a rectangle in the middle-bottom
    container[60:90, 50:100] = 1.0

    # Place a small square top-right
    container[5:25, 120:150] = 1.0

    return container


# =============================================================================
# Step 2: Verify Boundary Computation
# =============================================================================

def compute_boundary(raster):
    """
    Compute piece boundary: dilation XOR original

    This creates a 1-pixel ring around the piece.
    Used for contact scoring.
    """
    # Dilate the piece by 1 pixel
    dilated = binary_dilation_numpy(raster > 0, iterations=1)

    # XOR: pixels that are in dilated but NOT in original = boundary ring
    boundary = ((dilated > 0) & ~(raster > 0)).astype(np.float32)

    return boundary


def visualize_boundary(raster, boundary):
    """Visualize the piece and its boundary."""
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    axes[0].imshow(raster, cmap='Blues', origin='lower')
    axes[0].set_title('Original Piece')
    axes[0].set_xlabel('Pixels that will check for collision')

    axes[1].imshow(boundary, cmap='Reds', origin='lower')
    axes[1].set_title('Boundary (dilation XOR original)')
    axes[1].set_xlabel('1-pixel ring for contact scoring')

    # Overlay
    overlay = np.zeros((*raster.shape, 3))
    overlay[:,:,2] = raster  # Blue = piece interior
    overlay[:,:,0] = boundary  # Red = boundary
    axes[2].imshow(overlay, origin='lower')
    axes[2].set_title('Overlay (Blue=piece, Red=boundary)')
    axes[2].set_xlabel('Boundary touches neighbors when placed')

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / '1_boundary_verification.png', dpi=150)
    plt.close()
    print(f"Saved boundary visualization")

    # Verify boundary is correct
    piece_pixels = np.sum(raster > 0)
    boundary_pixels = np.sum(boundary > 0)
    print(f"  Piece pixels: {piece_pixels}")
    print(f"  Boundary pixels: {boundary_pixels}")
    print(f"  Boundary should be roughly the perimeter of the piece")


# =============================================================================
# Step 3: Verify Contact Map via FFT Convolution
# =============================================================================

def compute_contact_map(container, boundary):
    """
    Compute contact score for ALL positions via FFT convolution.

    contact_map[y, x] = how many boundary pixels would touch existing pieces
                        if we placed the piece at position (x, y)

    Higher contact = piece nestles into gaps better.
    """
    # FFT convolution: this computes contact for ALL positions in parallel!
    contact_map = cpu_fftconvolve(container, boundary, mode='valid')
    return contact_map


def visualize_contact_map(container, boundary, contact_map):
    """Visualize the contact scoring."""
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))

    axes[0].imshow(container, cmap='Greys', origin='lower')
    axes[0].set_title('Current Container State')
    axes[0].set_xlabel('Gray = existing pieces')

    axes[1].imshow(boundary, cmap='Reds', origin='lower')
    axes[1].set_title('Piece Boundary (kernel)')
    axes[1].set_xlabel('This gets convolved with container')

    im = axes[2].imshow(contact_map, cmap='hot', origin='lower')
    axes[2].set_title('Contact Map (FFT result)')
    axes[2].set_xlabel('Brighter = more contact with existing pieces')
    plt.colorbar(im, ax=axes[2], label='Contact score')

    # Mark the position with highest contact
    max_idx = np.unravel_index(np.argmax(contact_map), contact_map.shape)
    axes[2].plot(max_idx[1], max_idx[0], 'g*', markersize=15, label='Best contact')
    axes[2].legend()

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / '2_contact_map_verification.png', dpi=150)
    plt.close()
    print(f"Saved contact map visualization")

    print(f"  Contact map shape: {contact_map.shape}")
    print(f"  Contact range: {contact_map.min():.1f} to {contact_map.max():.1f}")
    print(f"  Best contact position: y={max_idx[0]}, x={max_idx[1]}")


# =============================================================================
# Step 4: Verify Valid Positions (Collision Detection)
# =============================================================================

def compute_valid_positions(container, piece):
    """
    Find all valid (non-colliding) positions via FFT convolution.

    This is the "Raster NFP": positions where piece can be placed.
    """
    # Convolve container with piece - overlap > 0 means collision
    overlap_map = cpu_fftconvolve(container, piece, mode='valid')

    # Valid positions have zero overlap
    valid_mask = overlap_map < 0.5

    return overlap_map, valid_mask


def visualize_valid_positions(container, piece, overlap_map, valid_mask):
    """Visualize collision detection."""
    fig, axes = plt.subplots(1, 4, figsize=(20, 5))

    axes[0].imshow(container, cmap='Greys', origin='lower')
    axes[0].set_title('Container')

    axes[1].imshow(piece, cmap='Blues', origin='lower')
    axes[1].set_title('Piece to place')

    axes[2].imshow(overlap_map, cmap='Reds', origin='lower')
    axes[2].set_title('Overlap Map (FFT result)')
    axes[2].set_xlabel('Red = collision, Black = valid')

    axes[3].imshow(valid_mask, cmap='Greens', origin='lower')
    axes[3].set_title('Valid Positions Mask')
    axes[3].set_xlabel(f'Green = can place here ({np.sum(valid_mask)} positions)')

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / '3_valid_positions_verification.png', dpi=150)
    plt.close()
    print(f"Saved valid positions visualization")

    print(f"  Overlap map shape: {overlap_map.shape}")
    print(f"  Valid positions: {np.sum(valid_mask)} out of {valid_mask.size}")


# =============================================================================
# Step 5: Verify Combined Score Map
# =============================================================================

def compute_score_map(valid_mask, contact_map, current_max_x, piece_width, contact_weight=2.0):
    """
    Compute combined score for ALL valid positions.

    score = strip_extension - contact * weight

    Lower score = better position
    - Minimizes strip extension (leftmost placement)
    - Maximizes contact (fills gaps)
    """
    result_h, result_w = valid_mask.shape

    # Strip extension for each x position
    x_coords = np.arange(result_w, dtype=np.float32)
    strip_extension = np.maximum(0, x_coords + piece_width - current_max_x)
    strip_extension_map = np.broadcast_to(
        strip_extension[None, :], (result_h, result_w)
    ).copy()

    # Combined score: minimize extension, maximize contact
    score_map = strip_extension_map - contact_map * contact_weight

    # Mask out invalid positions
    score_map_masked = np.where(valid_mask, score_map, np.float32(1e9))

    return score_map, score_map_masked, strip_extension_map


def visualize_score_map(valid_mask, contact_map, score_map, score_map_masked,
                        strip_ext_map, piece_width):
    """Visualize the final scoring."""
    fig, axes = plt.subplots(2, 3, figsize=(18, 10))

    # Row 1: Components
    axes[0,0].imshow(valid_mask, cmap='Greens', origin='lower')
    axes[0,0].set_title('Valid Mask')

    im1 = axes[0,1].imshow(contact_map, cmap='hot', origin='lower')
    axes[0,1].set_title('Contact Map (higher=better)')
    plt.colorbar(im1, ax=axes[0,1])

    im2 = axes[0,2].imshow(strip_ext_map, cmap='Blues', origin='lower')
    axes[0,2].set_title('Strip Extension (lower=better)')
    plt.colorbar(im2, ax=axes[0,2])

    # Row 2: Combined
    im3 = axes[1,0].imshow(score_map, cmap='RdYlGn_r', origin='lower')
    axes[1,0].set_title('Raw Score (lower=better)')
    plt.colorbar(im3, ax=axes[1,0])

    im4 = axes[1,1].imshow(score_map_masked, cmap='RdYlGn_r', origin='lower',
                           vmin=score_map[valid_mask].min(),
                           vmax=score_map[valid_mask].max())
    axes[1,1].set_title('Masked Score (invalid=white)')
    plt.colorbar(im4, ax=axes[1,1])

    # Find best position
    best_idx = np.unravel_index(np.argmin(score_map_masked), score_map_masked.shape)
    best_score = score_map_masked[best_idx]

    # Show best position
    axes[1,2].imshow(score_map_masked, cmap='RdYlGn_r', origin='lower',
                     vmin=score_map[valid_mask].min(),
                     vmax=score_map[valid_mask].max())
    axes[1,2].plot(best_idx[1], best_idx[0], 'b*', markersize=20, label='BEST')
    axes[1,2].set_title(f'Best Position: y={best_idx[0]}, x={best_idx[1]}, score={best_score:.1f}')
    axes[1,2].legend()

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / '4_score_map_verification.png', dpi=150)
    plt.close()
    print(f"Saved score map visualization")

    print(f"  Best position: y={best_idx[0]}, x={best_idx[1]}")
    print(f"  Best score: {best_score:.2f}")
    print(f"  Contact at best: {contact_map[best_idx]:.2f}")
    print(f"  Strip extension at best: {strip_ext_map[best_idx]:.2f}")


# =============================================================================
# Step 6: Verify Placement Result
# =============================================================================

def visualize_placement(container, piece, best_pos):
    """Show the piece placed at the best position."""
    y, x = best_pos
    ph, pw = piece.shape

    # Create result container
    result = container.copy()
    result[y:y+ph, x:x+pw] = np.maximum(result[y:y+ph, x:x+pw], piece)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    axes[0].imshow(container, cmap='Greys', origin='lower')
    axes[0].set_title('Before Placement')

    # Show placement with color coding
    rgb = np.zeros((*result.shape, 3))
    rgb[:,:,0] = container  # Red = original pieces
    rgb[:,:,2] = result - container  # Blue = new piece
    rgb[:,:,1] = container * 0.5  # Some green for contrast

    axes[1].imshow(rgb, origin='lower')
    axes[1].set_title(f'After Placement at y={y}, x={x}')
    axes[1].plot([x, x+pw, x+pw, x, x], [y, y, y+ph, y+ph, y], 'g-', linewidth=2)

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / '5_placement_result.png', dpi=150)
    plt.close()
    print(f"Saved placement visualization")


# =============================================================================
# Main Diagnostic
# =============================================================================

def main():
    print("=" * 70)
    print("RASTER NFP DIAGNOSTIC - Verifying Implementation")
    print("=" * 70)

    # Create test data
    print("\n[1] Creating test piece and container...")
    piece = create_test_piece()
    container = create_test_container_with_pieces()
    print(f"    Piece shape: {piece.shape}")
    print(f"    Container shape: {container.shape}")

    # Step 2: Boundary computation
    print("\n[2] Computing piece boundary (dilation XOR original)...")
    boundary = compute_boundary(piece)
    visualize_boundary(piece, boundary)

    # Verify boundary is a ring (not solid)
    if np.sum(boundary > 0) > np.sum(piece > 0):
        print("  WARNING: Boundary larger than piece - check dilation")
    if np.sum(boundary > 0) == 0:
        print("  ERROR: Boundary is empty!")
    else:
        print("  OK: Boundary looks correct (ring around piece)")

    # Step 3: Contact map
    print("\n[3] Computing contact map via FFT convolution...")
    contact_map = compute_contact_map(container, boundary)
    visualize_contact_map(container, boundary, contact_map)

    # Step 4: Valid positions
    print("\n[4] Computing valid positions (collision detection)...")
    overlap_map, valid_mask = compute_valid_positions(container, piece)
    visualize_valid_positions(container, piece, overlap_map, valid_mask)

    # Step 5: Score map
    print("\n[5] Computing combined score map...")
    current_max_x = 150  # Simulate current strip end
    piece_width = piece.shape[1]

    # Ensure shapes match
    if contact_map.shape != valid_mask.shape:
        print(f"  WARNING: Shape mismatch! contact_map={contact_map.shape}, valid_mask={valid_mask.shape}")
        # Crop to match
        min_h = min(contact_map.shape[0], valid_mask.shape[0])
        min_w = min(contact_map.shape[1], valid_mask.shape[1])
        contact_map = contact_map[:min_h, :min_w]
        valid_mask = valid_mask[:min_h, :min_w]
        print(f"  Cropped to {min_h}x{min_w}")

    score_map, score_map_masked, strip_ext_map = compute_score_map(
        valid_mask, contact_map, current_max_x, piece_width, contact_weight=2.0
    )
    visualize_score_map(valid_mask, contact_map, score_map, score_map_masked,
                        strip_ext_map, piece_width)

    # Step 6: Final placement
    print("\n[6] Visualizing best placement...")
    best_idx = np.unravel_index(np.argmin(score_map_masked), score_map_masked.shape)
    visualize_placement(container, piece, best_idx)

    # Summary
    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE")
    print("=" * 70)
    print(f"\nOutput saved to: {OUTPUT_DIR}/")
    print("\nCheck the PNG files to verify:")
    print("  1_boundary_verification.png - Boundary should be a RING, not solid")
    print("  2_contact_map_verification.png - Hot spots near existing pieces")
    print("  3_valid_positions_verification.png - Green = no collision")
    print("  4_score_map_verification.png - Best position balances contact + strip")
    print("  5_placement_result.png - Final placement looks reasonable")

    print("\nIf boundary is SOLID instead of a RING, the implementation is WRONG!")
    print("If contact_map is all zeros, FFT convolution isn't working!")


if __name__ == "__main__":
    main()
