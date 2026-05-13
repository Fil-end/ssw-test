import torch
from ase import Atoms
from ase.io import read, write
import numpy as np

class TorchLBFGSOptimizer:
    """基于 PyTorch LBFGS 的 ASE 优化器（支持 CUDA）"""
    def __init__(self, atoms: Atoms, calculator=None, 
                 lr=1.0, max_iter=20, history_size=100, 
                 tolerance_grad=1e-7, tolerance_change=1e-9,
                 device=None):
        
        self.atoms = atoms
        if calculator is not None:
            self.atoms.calc = calculator
        
        if device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = device
        
        # 把 positions 转为可优化的 torch 参数（在 GPU 上）
        positions = torch.tensor(self.atoms.get_positions(), 
                               dtype=torch.float64, 
                               device=self.device, 
                               requires_grad=True)
        self.positions = torch.nn.Parameter(positions)
        
        # PyTorch LBFGS 优化器
        self.optimizer = torch.optim.LBFGS(
            [self.positions],
            lr=lr,
            max_iter=max_iter,
            max_eval=None,
            tolerance_grad=tolerance_grad,
            tolerance_change=tolerance_change,
            history_size=history_size,
            line_search_fn=None  # 'strong_wolfe' 如果需要
        )
        
        self.converged = False
        self.step_count = 0

    def step(self):
        """执行一步优化"""
        def closure():
            self.optimizer.zero_grad()
            
            # 更新 ASE Atoms 的 positions（从 GPU 同步回 CPU）
            with torch.no_grad():
                pos_cpu = self.positions.detach().cpu().numpy()
                self.atoms.set_positions(pos_cpu)
            
            # 计算能量和力（通过 ASE Calculator）
            energy = self.atoms.get_potential_energy()
            forces = self.atoms.get_forces()   # shape: (N, 3)
            
            # 转为 torch tensor 并放到 GPU
            forces_torch = torch.tensor(forces, 
                                      dtype=torch.float64, 
                                      device=self.device)
            
            # 损失 = 能量（或 -能量，如果想最小化能量）
            loss = torch.tensor(energy, dtype=torch.float64, device=self.device)
            
            # 反向传播：力作为梯度（注意符号：力 = -∇E）
            self.positions.grad = -forces_torch
            
            return loss
        
        # 执行 LBFGS 一步
        loss = self.optimizer.step(closure)
        self.step_count += 1
        
        # 检查收敛（简单判断）
        if self.optimizer.state[self.positions]['prev_loss'] is not None:
            change = abs(loss.item() - self.optimizer.state[self.positions]['prev_loss'])
            if change < 1e-6:   # 可根据需要调整
                self.converged = True
        
        return loss.item()

    def run(self, fmax=0.05, steps=1000):
        """运行优化直到收敛或达到步数"""
        for _ in range(steps):
            self.step()
            max_force = np.max(np.abs(self.atoms.get_forces()))
            print(f"Step {self.step_count:4d} | Energy = {self.atoms.get_potential_energy():.6f} eV | "
                  f"Max Force = {max_force:.6f} eV/Å")
            
            if max_force < fmax:
                print("Optimization converged!")
                self.converged = True
                break
                
            if self.converged:
                break
        
        return self.atoms


# ====================== 使用示例 ======================
if __name__ == "__main__":
    # 示例：创建一个需要优化的体系（替换为你的体系）
    atoms = read("your_structure.xyz")          # 或 Atoms(...) 
    # atoms.calc = YourFastCalculator()         # 如 MACE, NequIP, ORB 等支持 GPU 的 ML 势
    
    optimizer = TorchLBFGSOptimizer(
        atoms=atoms,
        lr=1.0,
        max_iter=50,           # 每步内部 LBFGS 迭代次数
        history_size=100,
        device=torch.device("cuda")
    )
    
    optimized_atoms = optimizer.run(fmax=0.02, steps=500)
    write("optimized.xyz", optimized_atoms)