import sys
import os
os.add_dll_directory(r'D:\ProgramData\anaconda3\envs\Filend\bin')

import time
import torch
import numpy as np
import ase
from ase import Atoms
from mace.calculators import MACECalculator

def test_performance_comparison():
    print("=" * 70)
    print("Performance Comparison: BatchRelaxer vs batch_calculate_properties")
    print("=" * 70)

    model_path = r"D:\ECUST\bin\VLMC-FeC-main\mace-omat-0-medium.model"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    calculator = MACECalculator(
        model_paths=model_path,
        device=device,
        enable_cueq=False,
        dtype=torch.float32
    )
    model = calculator.models[0]
    model.eval()

    from mace import data as mace_data
    from mace.tools import torch_geometric
    from batch_relaxer import BatchRelaxer, batch_calculate_properties

    structures_file = r"d:\ECUST\Publication\Global_Optimization_Lib\SSW\ssw_output\ssw_structures.xyz"
    try:
        atoms_list = ase.io.read(structures_file, index=":")
    except Exception as e:
        print(f"Error reading: {e}")
        atoms_list = [Atoms('FeC', positions=[[0, 0, 0], [1.5, 0, 0]], cell=[10, 10, 10], pbc=True)]

    if not isinstance(atoms_list, list):
        atoms_list = [atoms_list]

    test_atoms = atoms_list[:20] if len(atoms_list) >= 20 else atoms_list
    print(f"Testing with {len(test_atoms)} structures, each with {len(test_atoms[0])} atoms")
    print()

    z_table = calculator.z_table
    r_max = calculator.r_max
    available_heads = calculator.available_heads

    batch_relaxer = BatchRelaxer(
        calculator=calculator,
        device=device,
        relax_cell=False,
        max_edges_per_batch=20000
    )

    print("-" * 70)
    print("Test 1: BatchRelaxer.relax with max_n_steps=0 (single-point)")
    print("-" * 70)
    start = time.time()
    relaxed_atoms = batch_relaxer.relax(
        atoms_list=test_atoms,
        fmax=0.1,
        max_n_steps=0,
        inplace=False
    )
    elapsed1 = time.time() - start
    print(f"Time: {elapsed1:.4f} s")
    print(f"Results: {len(relaxed_atoms)} structures")

    print()
    print("-" * 70)
    print("Test 2: batch_calculate_properties (parallel)")
    print("-" * 70)
    start = time.time()
    results = batch_calculate_properties(
        atoms_list=test_atoms,
        model=model,
        batch_size=8,
        device=device,
        num_workers=0,
        z_table=z_table,
        r_max=r_max,
        available_heads=available_heads,
        calc=calculator,
    )
    elapsed2 = time.time() - start
    print(f"Time: {elapsed2:.4f} s")
    print(f"Energies: {results['energies']}")

    print()
    print("-" * 70)
    print("Test 3: Sequential calculation (get_energy_forces)")
    print("-" * 70)
    from ase.calculators.mixing import SumCalculator
    from torch_dftd.torch_dftd3_calculator import TorchDFTD3Calculator

    d3_calc = TorchDFTD3Calculator(device=device, damping="zero", dtype=torch.float32, xc="pbe", cutoff=40.0)
    mace_calc = SumCalculator([calculator, d3_calc])

    test_atoms_copy = [at.copy() for at in test_atoms]
    start = time.time()
    for atoms in test_atoms_copy:
        atoms.calc = mace_calc
        _ = atoms.get_potential_energy()
        _ = atoms.get_forces()
    elapsed3 = time.time() - start
    print(f"Time: {elapsed3:.4f} s")

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"BatchRelaxer.relax (max_steps=0):     {elapsed1:.4f} s ({elapsed1/len(test_atoms)*1000:.2f} ms/structure)")
    print(f"batch_calculate_properties:          {elapsed2:.4f} s ({elapsed2/len(test_atoms)*1000:.2f} ms/structure)")
    print(f"Sequential (get_energy_forces):      {elapsed3:.4f} s ({elapsed3/len(test_atoms)*1000:.2f} ms/structure)")

    if elapsed2 < elapsed1:
        print(f"\nbatch_calculate_properties is {elapsed1/elapsed2:.2f}x faster than BatchRelaxer.relax")
    else:
        print(f"\nBatchRelaxer.relax is {elapsed2/elapsed1:.2f}x faster than batch_calculate_properties")

if __name__ == "__main__":
    test_performance_comparison()