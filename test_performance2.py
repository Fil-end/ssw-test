import sys
import os
os.add_dll_directory(r'D:\ProgramData\anaconda3\envs\Filend\bin')

import time
import torch
import numpy as np
import ase
from ase import Atoms
from mace.calculators import MACECalculator

def test_biased_dimer_rotation_performance():
    print("=" * 70)
    print("Testing biased_dimer_rotation Performance")
    print("=" * 70)

    model_path = r"D:\ECUST\bin\VLMC-FeC-main\mace-omat-0-medium.model"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    # 创建测试结构
    atoms = Atoms(
        'FeC',
        positions=[[0, 0, 0], [1.5, 0, 0]],
        cell=[10, 10, 10],
        pbc=True
    )

    from ssw import SSW

    ssw = SSW(
        atoms=atoms,
        mace_model_path=model_path,
        device=device,
        fixed_indices=None
    )

    # 测试biased_dimer_rotation性能
    N0 = np.random.normal(0, 1, 6)  # 2个原子，每个3个坐标
    N0 = N0 / np.linalg.norm(N0)

    print("\nTesting biased_dimer_rotation with batch_calculate_properties...")
    times = []
    for i in range(5):
        start = time.time()
        N = ssw.biased_dimer_rotation(atoms, N0)
        elapsed = time.time() - start
        times.append(elapsed)
        print(f"  Run {i+1}: {elapsed:.4f} s")

    print(f"\nAverage time: {np.mean(times):.4f} s")
    print(f"Std deviation: {np.std(times):.4f} s")

    # 测试批量计算性能
    print("\n" + "=" * 70)
    print("Testing batch_compute_forces Performance")
    print("=" * 70)

    # 创建多个测试结构
    atoms_list = []
    for i in range(10):
        at = atoms.copy()
        at.positions[1] = [1.5 + i*0.1, 0, 0]
        atoms_list.append(at)

    start = time.time()
    results = ssw._batch_compute_forces(atoms_list)
    elapsed = time.time() - start
    print(f"Batch compute time for 10 structures: {elapsed:.4f} s")
    print(f"Average per structure: {elapsed/10*1000:.2f} ms")
    print(f"Results obtained: {len(results)}")

if __name__ == "__main__":
    test_biased_dimer_rotation_performance()