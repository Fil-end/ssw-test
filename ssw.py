import numpy as np
from ase import Atoms
from ase.optimize import LBFGS
from ase.constraints import FixAtoms
from ase.io import write
import copy
import os
import time
import torch

from batch_relaxer import BatchRelaxer, batch_calculate_properties

# 添加CUDA DLL路径（你的环境）
os.add_dll_directory(r'D:\ProgramData\anaconda3\envs\Filend\bin')

class SSW:
    def __init__(self, atoms, temperature=300.0, ds=0.1, max_gaussians=25, 
                 mc_temp=1000.0, mace_model_path=None, device='cuda',
                 Y_target=0.02, lambda_step=1.8, xi=0.2, 
                 fixed_indices=None):   # LS-SSW 参数
        self.atoms = atoms.copy()
        self.temperature = temperature
        self.ds = ds
        self.max_gaussians = max_gaussians
        
        self.fixed_indices = fixed_indices if fixed_indices is not None else []
        self.mc_temp = mc_temp
        self.kB = 8.617333262145e-5
        self.mace_model_path = mace_model_path
        self.device = device
        self.mace_calculator = None
        
        # LS-SSW 参数
        self.Y_target = Y_target      # 目标平均罚势能量 (eV/atom)，全局优化用小值 (0.02~0.1)，反应采样可用较大值
        self.lambda_step = lambda_step
        self.xi = xi
        self.A_pq = None              # 将在第一次调用时初始化
        
        # 初始化 MACE 计算器
        try:
            from mace.calculators import MACECalculator
            from ase.calculators.mixing import SumCalculator
            from torch_dftd.torch_dftd3_calculator import TorchDFTD3Calculator
            
            if not os.path.exists(self.mace_model_path):
                raise FileNotFoundError(f"MACE model not found: {self.mace_model_path}")
            
            if self.device == 'cuda' and not torch.cuda.is_available():
                print("CUDA not available, switching to CPU")
                self.device = 'cpu'
            
            # 保存原始MACE计算器引用
            self.mace_model_calculator = MACECalculator(
                model_paths=self.mace_model_path, 
                device=self.device, 
                enable_cueq=False, 
                dtype=torch.float32
            )
            
            d3_calc = TorchDFTD3Calculator(device=self.device, damping="zero",
                                           dtype=torch.float32, xc="pbe", cutoff=40.0)
            self.mace_calculator = SumCalculator([self.mace_model_calculator, d3_calc])
            print(f"MACE calculator initialized on {self.device}")

            # 初始化BatchRelaxer用于批量计算
            self.batch_relaxer = BatchRelaxer(
                calculator=self.mace_model_calculator,
                device=self.device,
                relax_cell=False,
                max_edges_per_batch = 30000
            )
            print("BatchRelaxer initialized")
        except Exception as e:
            raise RuntimeError(f"Failed to initialize MACE: {e}")

    def get_energy_forces(self, atoms, apply_softening=False):
        """支持是否添加局部软化罚势"""
        atoms.calc = self.mace_calculator
        e = atoms.get_potential_energy()
        f = atoms.get_forces()
        
        if apply_softening and self.A_pq is not None:
            penalty_e, penalty_f = self._compute_penalty(atoms)
            e += penalty_e
            f -= penalty_f  # 罚势是排斥，力方向相反
        
        return e, f
    
    def _batch_compute_forces(self, atoms_list):
        """批量计算多个原子结构的力（使用batch_calculate_properties并行计算）"""
        try:
            # 使用batch_calculate_properties进行批量并行计算
            results_dict = batch_calculate_properties(
                atoms_list=atoms_list,
                model=self.mace_model_calculator.models[0],
                batch_size=8,
                device=self.device,
                num_workers=0,
                z_table=self.mace_model_calculator.z_table,
                r_max=self.mace_model_calculator.r_max,
                available_heads=self.mace_model_calculator.available_heads,
                calc=self.mace_model_calculator,
            )

            # 组合能量和力
            results = []
            for i in range(len(results_dict['energies'])):
                energy = results_dict['energies'][i]
                forces = results_dict['forces'][i]
                results.append((energy, forces))

            return results
        except Exception as e:
            print(f"Batch compute failed: {e}")
            import traceback
            traceback.print_exc()
            # 回退到逐个计算
            results = []
            for atoms in atoms_list:
                e, f = self.get_energy_forces(atoms, apply_softening=False)
                results.append((e, f))
            return results

    def _compute_penalty(self, atoms):
        """计算 Buckingham 罚势能量和力（只对距离 < 3.0 Å 的对作用）"""
        if self.A_pq is None:
            self.A_pq = {}
            n = len(atoms)
            for i in range(n):
                for j in range(i+1, n):
                    self.A_pq[(i,j)] = 0.03 * 3.61  # 初始 3% C-C 键能
        
        pos = atoms.positions
        n = len(pos)
        penalty_e = 0.0
        penalty_f = np.zeros_like(pos)
        
        for i in range(n):
            for j in range(i+1, n):
                rij_vec = pos[j] - pos[i]
                r = np.linalg.norm(rij_vec)
                if r < 3.0 and r > 1e-6:
                    r0 = r
                    A = self.A_pq.get((i,j), 0.03 * 3.61)  # 默认初始强度
                    exp_term = np.exp(-(r - r0) / (self.xi * r0))
                    Vp = 0.5 * A * exp_term
                    
                    penalty_e += Vp
                    
                    # 力：对原子 i 和 j
                    force_mag = (A / (self.xi * r0)) * exp_term
                    f_dir = rij_vec / r
                    penalty_f[i] += force_mag * f_dir
                    penalty_f[j] -= force_mag * f_dir
        
        return penalty_e, penalty_f

    def local_relaxation(self, atoms:Atoms):
        current_atoms = atoms.copy()
        # 确保应用约束
        if len(self.fixed_indices) > 0:
            current_atoms.set_constraint(FixAtoms(indices=self.fixed_indices))
        current_atoms.calc = self.mace_calculator
        
        # 使用ASE的LBFGS优化器
        dyn = LBFGS(current_atoms)
        dyn.run(fmax=0.05, steps=300)
        
        return current_atoms
    
    def normalize(self, v):
        norm = np.linalg.norm(v)
        return v / norm if norm > 1e-12 else v

    def biased_dimer_rotation(self, atoms, N0, deltaR=0.01, a=100.0, max_iter=15):
        """biased dimer rotation（论文核心软化步骤，使用批量计算优化）"""
        R0 = atoms.positions.copy().flatten()
        N = self.normalize(N0.copy())

        # 应用约束到输入原子
        if len(self.fixed_indices) > 0:
            atoms.set_constraint(FixAtoms(indices=self.fixed_indices))

        # 计算初始力
        _, F0 = self.get_energy_forces(atoms, apply_softening=False)
        F0 = F0.flatten()

        start_time = time.time()

        # 预计算固定原子掩码
        fixed_mask = np.zeros(len(R0), dtype=bool)
        for idx in self.fixed_indices:
            fixed_mask[idx*3:idx*3+3] = True

        # 优化：使用ASE的copy方法
        base_atoms = atoms.copy()
        
        # 预准备所有原子结构用于批量计算
        atoms_list = []
        for k in range(max_iter):
            R_k = R0 + deltaR * N
            # 快速应用固定原子约束
            if len(self.fixed_indices) > 0:
                R_k[fixed_mask] = R0[fixed_mask]
            
            # 优化：复用base_atoms
            atoms_k = base_atoms.copy()
            atoms_k.positions = R_k.reshape(-1, 3)
            if len(self.fixed_indices) > 0:
                atoms_k.set_constraint(FixAtoms(indices=self.fixed_indices))
            atoms_list.append(atoms_k)
        
        # 批量计算所有结构的能量和力
        results = self._batch_compute_forces(atoms_list)
        
        # 更智能的收敛判断
        converged = False
        for i in range(max_iter):
            E1, F1 = results[i]
            F1 = F1.flatten()
            
            perp_F = (F0 - F1) - np.dot((F0 - F1), N) * N
            perp_norm = np.linalg.norm(perp_F)
            
            if perp_norm < 1e-6:
                converged = True
                break
            elif perp_norm < 1e-4 and i > 3:
                # 接近收敛时提前停止
                break
                
            R1 = R0 + deltaR * N
            bias_force = -a * np.dot((R1 - R0), N0) * N0
            N_new = self.normalize(perp_F + bias_force)
            N = self.normalize(0.5 * N + 0.5 * N_new)
        
        # 清理内存
        del atoms_list, results
        import gc
        gc.collect()
        
        print(f"biased_dimer_rotation time: {time.time() - start_time:.4f} s (iterations: {i+1})")
        return N

    def update_A_pq(self, atoms, penalty_energy_per_atom):
        """自适应调节 A_pq（LS-SSW 核心）"""
        if self.A_pq is None:
            # 初始化
            self.A_pq = {}
            n = len(atoms)
            for i in range(n):
                for j in range(i+1, n):
                    self.A_pq[(i,j)] = 0.03 * 3.61  # 初始 3% C-C 键能
        
        # 简单自适应：整体缩放（实际论文更精细，这里简化但有效）
        scale = 1.0 - self.lambda_step * (penalty_energy_per_atom - self.Y_target) / 10.0
        scale = np.clip(scale, 0.5, 2.0)
        
        for k in self.A_pq:
            self.A_pq[k] *= scale

    def climb(self, atoms, ls_ssw=False):
        """核心 climbing，支持 LS-SSW"""
        # 确保新的原子结构继承约束
        atoms = atoms.copy()
        # 应用约束
        if len(self.fixed_indices) > 0:
            atoms.set_constraint(FixAtoms(indices=self.fixed_indices))
        
        E_m, _ = self.get_energy_forces(atoms, apply_softening=False)
        
        # === LS-SSW: 应用局部软化 ===
        if ls_ssw:
            penalty_e, _ = self._compute_penalty(atoms)
            penalty_per_atom = penalty_e / len(atoms)
            self.update_A_pq(atoms, penalty_per_atom)  # 自适应
        
        # 生成初始随机模式
        R_m = atoms.positions.copy().flatten()
        N_global = np.random.normal(0, 1, R_m.size)
        N_global = self.normalize(N_global)
        
        idx = np.random.choice(len(atoms), 2, replace=False)
        qa = atoms.positions[idx[0]]
        qb = atoms.positions[idx[1]]
        N_local = np.zeros(R_m.size)
        N_local[idx[0]*3:idx[0]*3+3] = qb - qa
        N_local[idx[1]*3:idx[1]*3+3] = qa - qb
        N_local = self.normalize(N_local)
        
        lam = np.random.uniform(0.1, 1.5)
        N0 = self.normalize(N_global + lam * N_local)
        
        N = self.biased_dimer_rotation(atoms, N0)

        current_atoms = atoms.copy()
        # 确保current_atoms也应用了约束
        if len(self.fixed_indices) > 0:
            current_atoms.set_constraint(FixAtoms(indices=self.fixed_indices))

        for n in range(1, self.max_gaussians + 1):
            disp = self.ds * N.reshape(-1, 3)
            # 强制固定原子的位移为[0.0, 0.0, 0.0]
            if len(self.fixed_indices) > 0:
                disp[self.fixed_indices] = [0.0, 0.0, 0.0]
            current_atoms.positions += disp
            
            N = self.biased_dimer_rotation(current_atoms, N)
            
            E_cur, _ = self.get_energy_forces(current_atoms, apply_softening=ls_ssw)
            if E_cur < E_m - 0.01:   # 提前停止
                break
        
        # 移除罚势，全局松弛到真实 PES 上的新极小点
        final_atoms = current_atoms.copy()
        final_atoms = self.local_relaxation(final_atoms)
        E_new, _ = self.get_energy_forces(final_atoms, apply_softening=False)
        return final_atoms, E_new

    def run(self, max_steps=1000, ls_ssw=False):
        """真正集成 LS-SSW"""
        trajectory = []
        current_atoms = self.atoms.copy()
        
        # 先对局域优化输入结构
        print("=== Performing initial local relaxation ===")
        current_atoms = self.local_relaxation(current_atoms)
        E_current, _ = self.get_energy_forces(current_atoms, apply_softening=False)
        print(f"Initial relaxed energy: {E_current:.4f} eV")
        
        trajectory.append((current_atoms.copy(), E_current))
        
        # 创建输出目录
        output_dir = 'ssw_output'
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        traj_type = 'LS-SSW' if ls_ssw else 'SSW'
        traj_file = os.path.join(output_dir, f'{traj_type.lower()}_structures.xyz')
        energy_file = os.path.join(output_dir, f'{traj_type.lower()}_energies.txt')
        
        # 写入初始结构
        with open(traj_file, 'w') as traj_handle:
            write(traj_handle, current_atoms, format='extxyz')
        
        # 写入初始能量
        with open(energy_file, 'w') as energy_handle:
            energy_handle.write("# Step | Energy (eV) | Status\n")
            energy_handle.write(f"{0:4d} | {E_current:12.6f} | Initial\n")
        
        for step in range(max_steps):
            new_atoms, E_new = self.climb(current_atoms, ls_ssw=ls_ssw)
            
            # Metropolis Monte Carlo
            if E_new < E_current:
                accept = True
            else:
                deltaE = E_new - E_current
                prob = np.exp(-deltaE / (self.kB * self.mc_temp))
                accept = np.random.rand() < prob
            
            if accept:
                current_atoms = new_atoms
                E_current = E_new
                trajectory.append((current_atoms.copy(), E_current))
                # 追加保存结构到xyz文件
                with open(traj_file, 'a') as traj_handle:
                    write(traj_handle, current_atoms, format='extxyz')
                status = "LS-SSW" if ls_ssw else "SSW"
                print(f"Step {step+1:4d} | {status} | Accepted | E = {E_current:8.4f} eV")
                # 追加能量到文件
                with open(energy_file, 'a') as energy_handle:
                    energy_handle.write(f"{step+1:4d} | {E_current:12.6f} | Accepted\n")
            else:
                print(f"Step {step+1:4d} | {'LS-SSW' if ls_ssw else 'SSW'} | Rejected | E = {E_new:8.4f} eV")
        
        print(f"Structure saved to {traj_file}")
        print(f"Energy curve saved to {energy_file}")
        
        return trajectory


# ====================== 使用示例 ======================
if __name__ == "__main__":
    from ase.io.dmol import read_dmol_arc
    
    atoms = read_dmol_arc("opt_input.arc")
    
    # 在外部固定原子：根据z坐标排序，选择最底层的25个原子
    z_coords = atoms.positions[:, 2]
    sorted_indices = np.argsort(z_coords)
    fixed_indices = sorted_indices[:25].tolist()
    atoms.set_constraint(FixAtoms(indices=fixed_indices))
    print(f"Fixed {len(fixed_indices)} atoms with lowest z-coordinates: {fixed_indices}")
    
    mace_model_path = r'D:\ECUST\bin\VLMC-FeC-main\mace-omat-0-medium.model'
    
    # 创建SSW，传入fixed_indices
    ssw = SSW(atoms, mc_temp=200.0, ds=0.6, max_gaussians=30, 
              mace_model_path=mace_model_path, device='cuda', 
              Y_target=0.1, fixed_indices=fixed_indices)   # 全局优化建议用较小 Y
    
    # traj_ssw = ssw.run(max_steps=200, ls_ssw=False)
    
    print("\n=== Running LS-SSW with MACE (Local Softening + Adaptive) ===")
    traj_lssw = ssw.run(max_steps=500, ls_ssw=True)
    
    min_e_ssw = min([e for _, e in traj_ssw])
    min_e_lssw = min([e for _, e in traj_lssw])
    
    print(f"\nSSW   min energy: {min_e_ssw:.4f} eV")
    print(f"LS-SSW min energy: {min_e_lssw:.4f} eV  (should be lower or found faster)")
    
    # 保存能量曲线数据
    import matplotlib.pyplot as plt
    
    # 提取能量数据
    steps_ssw = list(range(len(traj_ssw)))
    energies_ssw = [e for _, e in traj_ssw]
    steps_lssw = list(range(len(traj_lssw)))
    energies_lssw = [e for _, e in traj_lssw]
    
    # 保存能量曲线数据到文件
    energy_data_file = 'ssw_output/energy_curves.txt'
    with open(energy_data_file, 'w') as f:
        f.write("# SSW Energy Curve\n")
        f.write("# Step Energy(eV)\n")
        for step, energy in zip(steps_ssw, energies_ssw):
            f.write(f"{step:4d} {energy:12.6f}\n")
        f.write("\n# LS-SSW Energy Curve\n")
        f.write("# Step Energy(eV)\n")
        for step, energy in zip(steps_lssw, energies_lssw):
            f.write(f"{step:4d} {energy:12.6f}\n")
    
    # 绘制能量曲线
    plt.figure(figsize=(10, 6))
    plt.plot(steps_ssw, energies_ssw, 'b-', label='SSW', linewidth=1.5)
    plt.plot(steps_lssw, energies_lssw, 'r-', label='LS-SSW', linewidth=1.5)
    plt.xlabel('Step', fontsize=12)
    plt.ylabel('Energy (eV)', fontsize=12)
    plt.title('Global Optimization Energy Curves', fontsize=14)
    plt.legend(fontsize=11)
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig('ssw_output/energy_curves.png', dpi=150)
    print(f"\nEnergy curve plot saved to ssw_output/energy_curves.png")
    print(f"Energy data saved to {energy_data_file}")