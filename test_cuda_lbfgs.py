import sys
import os
os.add_dll_directory(r'D:\ProgramData\anaconda3\envs\Filend\bin')

import time
import torch
import numpy as np
import ase
from ase import Atoms
from ase.optimize import LBFGS as ASE_LBFGS
from mace.calculators import MACECalculator
from ase.calculators.mixing import SumCalculator
from torch_dftd.torch_dftd3_calculator import TorchDFTD3Calculator

from cuda_lbfgs import TorchLBFGSOptimizer


def setup_calculator(device):
    model_path = r"D:\ECUST\bin\VLMC-FeC-main\mace-omat-0-medium.model"
    mace_calc = MACECalculator(
        model_paths=model_path,
        device=device,
        enable_cueq=False,
        dtype=torch.float32
    )
    d3_calc = TorchDFTD3Calculator(device=device, damping="zero", dtype=torch.float32, xc="pbe", cutoff=40.0)
    return SumCalculator([mace_calc, d3_calc])


def test_cuda_lbfgs_vs_ase_lbfgs():
    print("=" * 70)
    print("Performance Comparison: CUDA-LBFGS vs ASE-LBFGS")
    print("=" * 70)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")
    if device == "cpu":
        print("WARNING: CUDA not available, comparison will be CPU vs CPU")

    structures_file = r"d:\ECUST\Publication\Global_Optimization_Lib\SSW\ssw_output\ssw_structures.xyz"

    try:
        atoms_list = ase.io.read(structures_file, index=":")
    except Exception as e:
        print(f"Error reading {structures_file}: {e}")
        print("Creating dummy test structure instead")
        atoms_list = [Atoms('FeC', positions=[[0, 0, 0], [1.5, 0, 0]], cell=[10, 10, 10], pbc=True)]

    if not isinstance(atoms_list, list):
        atoms_list = [atoms_list]

    n_structures = min(10, len(atoms_list))
    test_indices = [0, 50, 100, 150, 200, 250, 300, 350, 400, 450][:n_structures]
    test_atoms = [atoms_list[i] if i < len(atoms_list) else atoms_list[0] for i in test_indices]

    print(f"Testing with {len(test_atoms)} structures")
    print(f"Each structure has {len(test_atoms[0])} atoms")
    print()

    calculator = setup_calculator(device)
    for atoms in test_atoms:
        atoms.calc = calculator

    fmax = 0.05
    max_steps = 100

    print("-" * 70)
    print("ASE LBFGS Optimization (CPU-based)")
    print("-" * 70)

    ase_times = []
    ase_final_energies = []
    ase_final_forces = []
    ase_converged_counts = []

    for i, atoms in enumerate(test_atoms):
        atoms_copy = atoms.copy()
        atoms_copy.calc = calculator

        start = time.time()
        dyn = ASE_LBFGS(atoms_copy)
        dyn.run(fmax=fmax, steps=max_steps)
        elapsed = time.time() - start

        forces = atoms_copy.get_forces()
        max_force = np.max(np.abs(forces))
        energy = atoms_copy.get_potential_energy()
        converged = max_force < fmax

        ase_times.append(elapsed)
        ase_final_energies.append(energy)
        ase_final_forces.append(max_force)
        ase_converged_counts.append(1 if converged else 0)

        print(f"Structure {i+1:2d}: Time={elapsed:7.3f}s | E={energy:12.6f} eV | "
              f"MaxF={max_force:8.5f} eV/A | {'CONVERGED' if converged else 'MAX_STEPS'}")

    print()
    print("-" * 70)
    print("CUDA LBFGS Optimization (GPU-accelerated via PyTorch)")
    print("-" * 70)

    cuda_times = []
    cuda_final_energies = []
    cuda_final_forces = []
    cuda_converged_counts = []

    for i, atoms in enumerate(test_atoms):
        atoms_copy = atoms.copy()
        atoms_copy.calc = calculator

        start = time.time()
        optimizer = TorchLBFGSOptimizer(
            atoms=atoms_copy,
            calculator=calculator,
            lr=1.0,
            max_iter=20,
            history_size=100,
            tolerance_grad=1e-7,
            tolerance_change=1e-9,
            device=torch.device(device)
        )
        optimizer.run(fmax=fmax, steps=max_steps)
        elapsed = time.time() - start

        forces = atoms_copy.get_forces()
        max_force = np.max(np.abs(forces))
        energy = atoms_copy.get_potential_energy()
        converged = max_force < fmax

        cuda_times.append(elapsed)
        cuda_final_energies.append(energy)
        cuda_final_forces.append(max_force)
        cuda_converged_counts.append(1 if converged else 0)

        print(f"Structure {i+1:2d}: Time={elapsed:7.3f}s | E={energy:12.6f} eV | "
              f"MaxF={max_force:8.5f} eV/A | {'CONVERGED' if converged else 'MAX_STEPS'}")

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    total_ase_time = sum(ase_times)
    total_cuda_time = sum(cuda_times)
    avg_ase_time = np.mean(ase_times)
    avg_cuda_time = np.mean(cuda_times)

    ase_energy_mean = np.mean(ase_final_energies)
    cuda_energy_mean = np.mean(cuda_final_energies)
    energy_diff = np.abs(np.array(ase_final_energies) - np.array(cuda_final_energies))
    max_energy_diff = np.max(energy_diff)
    mean_energy_diff = np.mean(energy_diff)

    ase_force_mean = np.mean(ase_final_forces)
    cuda_force_mean = np.mean(cuda_final_forces)

    ase_converged = sum(ase_converged_counts)
    cuda_converged = sum(cuda_converged_counts)

    print(f"\n{'Metric':<30} {'ASE LBFGS':>15} {'CUDA LBFGS':>15}")
    print("-" * 62)
    print(f"{'Total Time (s)':<30} {total_ase_time:>15.3f} {total_cuda_time:>15.3f}")
    print(f"{'Avg Time per Structure (s)':<30} {avg_ase_time:>15.3f} {avg_cuda_time:>15.3f}")
    print(f"{'Final Energy Mean (eV)':<30} {ase_energy_mean:>15.6f} {cuda_energy_mean:>15.6f}")
    print(f"{'Final Max Force Mean (eV/A)':<30} {ase_force_mean:>15.6f} {cuda_force_mean:>15.6f}")
    print(f"{'Converged Structures':<30} {ase_converged:>15} {cuda_converged:>15}")

    print(f"\n{'Energy Difference Analysis:':}")
    print(f"  Max Energy Difference: {max_energy_diff:.8f} eV")
    print(f"  Mean Energy Difference: {mean_energy_diff:.8f} eV")

    if total_cuda_time < total_ase_time:
        speedup = total_ase_time / total_cuda_time
        print(f"\nCUDA LBFGS is {speedup:.2f}x FASTER than ASE LBFGS")
    else:
        slowdown = total_cuda_time / total_ase_time
        print(f"\nASE LBFGS is {slowdown:.2f}x FASTER than CUDA LBFGS")
        print("(This may be expected if structures are small or GPU overhead dominates)")

    print()
    print("=" * 70)
    print("Per-Structure Comparison")
    print("=" * 70)
    print(f"{'#':>3} {'ASE Time':>10} {'CUDA Time':>10} {'Speedup':>10} "
          f"{'ASE E':>14} {'CUDA E':>14} {'E Diff':>12}")
    print("-" * 80)
    for i in range(len(test_atoms)):
        spd = ase_times[i] / cuda_times[i] if cuda_times[i] > 1e-6 else 0
        ediff = abs(ase_final_energies[i] - cuda_final_energies[i])
        print(f"{i+1:3d} {ase_times[i]:10.3f} {cuda_times[i]:10.3f} {spd:10.2f}x "
              f"{ase_final_energies[i]:14.6f} {cuda_final_energies[i]:14.6f} {ediff:12.8f}")

    return {
        'ase_times': ase_times,
        'cuda_times': cuda_times,
        'ase_energies': ase_final_energies,
        'cuda_energies': cuda_final_energies,
        'ase_forces': ase_final_forces,
        'cuda_forces': cuda_final_forces,
        'total_ase_time': total_ase_time,
        'total_cuda_time': total_cuda_time,
        'speedup': total_ase_time / total_cuda_time if total_cuda_time > 1e-6 else 0
    }


if __name__ == "__main__":
    results = test_cuda_lbfgs_vs_ase_lbfgs()
    print("\nTest completed successfully!")
