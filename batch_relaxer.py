# All code are copied from https://github.com/Phorbol/mace/blob/batch-relaxer/mace/calculators/batch_relaxer.py
import os
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

from ase import Atoms
from ase.calculators.singlepoint import SinglePointCalculator
from ase.filters import FrechetCellFilter
from ase.optimize import FIRE
from ase.optimize.optimize import Optimizer
from ase.stress import full_3x3_to_voigt_6_stress
from ase.io import Trajectory 
# 引入 ase.io.write 用于流式输出
from mace import data
from mace.tools import torch_geometric
import torch
import numpy as np
from mace import data as mace_data
from mace.tools import torch_geometric

logger = logging.getLogger("MACE_BatchRelax")

def _get_mace_config_and_data(atoms: Atoms, calculator, heads: List[str]) -> data.AtomicData:
    """Helper to generate MACE AtomicData from ASE Atoms."""
    key_spec = data.KeySpecification(
        info_keys={},
        arrays_keys={calculator.charges_key: "Qs"} if hasattr(calculator, "charges_key") else {}
    )

    config = data.config_from_atoms(
        atoms,
        key_specification=key_spec,
        head_name=heads 
    )

    atomic_data = data.AtomicData.from_config(
        config,
        z_table=calculator.z_table,
        cutoff=calculator.r_max,
        heads=heads, 
    )
    return atomic_data

class RelaxBatch:
    """Internal worker class to manage a dynamic batch of optimizers."""

    def __init__(
        self,
        calculator,
        optimizer_cls=FIRE,
        fmax: float = 0.01,
        atoms_filter_cls=None,
        max_n_steps: int = 500,
        device: str = 'cuda',
        optimizer_kwargs: Dict = None,
        target_heads: List[str] = None 
    ):
        self.calc = calculator
        self.model = calculator.models[0]
        self.optimizer_cls = optimizer_cls
        self.fmax = fmax
        self.atoms_filter_cls = atoms_filter_cls
        self.max_n_steps = max_n_steps
        self.device = device
        self.optimizer_kwargs = optimizer_kwargs or {}
        self.target_heads = target_heads if target_heads else ["Default"]

        # Batch State
        self.opt_list: List[Optimizer] = []
        self.all_atoms: List[Atoms] = [] 
        self.edge_counts: List[int] = [] 
        self.opt_flags: List[bool] = []  
        self.ids: List[Any] = []
        
        self.trajectories: List[Union[Trajectory, None]] = []
        self.total_edges: int = 0

    @property
    def num_active(self) -> int:
        return sum(self.opt_flags)

    def insert(self, atoms: Atoms, num_edges: int, idx: Any, logfile=None, traj_file=None) -> None:
        """Insert a new atoms object into the batch."""
        atoms.calc = SinglePointCalculator(atoms)

        if self.atoms_filter_cls:
            filtered_atoms = self.atoms_filter_cls(atoms)
        else:
            filtered_atoms = atoms

        # --- 参数覆盖逻辑 (从 atoms.info 读取 opt_kwargs) ---
        final_kwargs = self.optimizer_kwargs.copy()
        if 'opt_kwargs' in atoms.info and isinstance(atoms.info['opt_kwargs'], dict):
            final_kwargs.update(atoms.info['opt_kwargs'])
        # -----------------------------------------------

        opt = self.optimizer_cls(
            filtered_atoms,
            logfile=logfile, 
            trajectory=None, # 禁用内部 Trajectory，手动管理
            **final_kwargs
        )
        opt.fmax = self.fmax

        # --- 手动 Trajectory 管理 ---
        # 只有当 traj_file 不为 None 时，才记录过程
        traj_handler = None
        if traj_file:
            traj_handler = Trajectory(traj_file, 'w', atoms)
            traj_handler.write(atoms)
        # --------------------------

        self.opt_list.append(opt)
        self.all_atoms.append(atoms)
        self.edge_counts.append(num_edges)
        self.opt_flags.append(True)
        self.ids.append(idx)
        self.trajectories.append(traj_handler)
        
        self.total_edges += num_edges

    def pop_converged(self) -> List[Tuple[Any, Atoms]]:
        """Removes converged atoms and closes their trajectory files."""
        new_opt_list = []
        new_all_atoms = []
        new_edge_counts = []
        new_ids = []
        new_trajectories = []
        
        converged_items = []

        for i in range(len(self.opt_list)):
            if self.opt_flags[i]:
                new_opt_list.append(self.opt_list[i])
                new_all_atoms.append(self.all_atoms[i])
                new_edge_counts.append(self.edge_counts[i])
                new_ids.append(self.ids[i])
                new_trajectories.append(self.trajectories[i])
            else:
                converged_items.append((self.ids[i], self.all_atoms[i]))
                if self.trajectories[i] is not None:
                    self.trajectories[i].close()

        self.opt_list = new_opt_list
        self.all_atoms = new_all_atoms
        self.edge_counts = new_edge_counts
        self.ids = new_ids
        self.trajectories = new_trajectories
        self.opt_flags = [True] * len(self.opt_list)
        self.total_edges = sum(self.edge_counts)

        return converged_items

    def step(self):
        """Performs one optimization step."""
        if not self.opt_list:
            return

        # 1. 解包真实原子 (Fix: Shape Mismatch for Cell Relax)
        real_atoms_list = []
        for opt in self.opt_list:
            if self.atoms_filter_cls:
                real_atoms_list.append(opt.atoms.atoms)
            else:
                real_atoms_list.append(opt.atoms)

        data_list = [
            _get_mace_config_and_data(atoms, self.calc, heads=self.target_heads)
            for atoms in real_atoms_list
        ]

        loader = torch_geometric.dataloader.DataLoader(
            dataset=data_list,
            batch_size=len(data_list),
            shuffle=False,
            drop_last=False
        )
        batch = next(iter(loader)).to(self.device)

        # 2. Compute
        batch_clone = batch.clone()
        use_compile = getattr(self.calc, "use_compile", False)
        
        batch_clone["node_attrs"].requires_grad_(True)
        batch_clone["positions"].requires_grad_(True)
        
        compute_stress = (self.calc.model_type in ["MACE", "EnergyDipoleMACE"]) and (not use_compile)
        
        out = self.model(
            batch_clone.to_dict(),
            compute_stress=compute_stress,
            training=use_compile
        )

        energies = out["energy"].detach().cpu().numpy()
        node_forces = out["forces"].detach().cpu().numpy()
        stresses = out["stress"].detach().cpu().numpy() if compute_stress else None

        pointer = 0
        
        for i, opt in enumerate(self.opt_list):
            target_atoms = real_atoms_list[i]
            n_atoms = len(target_atoms)
            
            e = energies[i] * self.calc.energy_units_to_eV
            f = node_forces[pointer : pointer + n_atoms] * self.calc.energy_units_to_eV / self.calc.length_units_to_A
            pointer += n_atoms
            
            s = None
            if stresses is not None:
                s = full_3x3_to_voigt_6_stress(
                    stresses[i] * self.calc.energy_units_to_eV / self.calc.length_units_to_A**3
                )

            target_atoms.calc = SinglePointCalculator(
                target_atoms, energy=e, forces=f, stress=s
            )

            current_f = opt.atoms.get_forces().flatten()
            step_count = getattr(opt, "nsteps", 0) 
            
            if opt.converged(current_f) or (step_count >= self.max_n_steps):
                self.opt_flags[i] = False
            else:
                opt.step()
                # 仅当 trajectories[i] 存在时才写入
                if self.trajectories[i] is not None:
                    self.trajectories[i].write(target_atoms)

class BatchRelaxer:
    """Main Interface for MACE Batch Relaxation."""

    def __init__(
        self,
        calculator,
        optimizer_cls=FIRE,
        max_edges_per_batch: int = 30000,
        relax_cell: bool = False, 
        device: str = 'cuda'
    ):
        self.calc = calculator
        self.optimizer_cls = optimizer_cls
        self.max_edges = max_edges_per_batch
        self.default_relax_cell = relax_cell
        self.device = device
        
        if len(calculator.models) != 1:
            raise ValueError("BatchRelaxer only supports single-model calculators.")


    def relax(
        self, 
        atoms_list: List[Atoms], 
        fmax: float = 0.02, 
        relax_cell: Optional[bool] = None, 
        head: Optional[str] = None, 
        max_n_steps: int = 200,
        inplace: bool = True,
        
        # --- 路径控制参数 ---
        trajectory_dir: Optional[str] = None,         # 过程轨迹 (.traj) 存放目录，None 为不保存
        append_trajectory_file: Optional[str] = None, # 最终结果 (.xyz) 流式输出路径，None 为不保存
        # ------------------

        save_log_file: Optional[str] = None,
        verbose: bool = False,
        optimizer_kwargs: Dict = None
    ) -> List[Atoms]:
        """
        Run batch relaxation with dynamic batching, multi-gpu support, and streaming I/O.
        """
        
        # --- 1. 确定是否使用 Cell Relax ---
        use_relax_cell = relax_cell if relax_cell is not None else self.default_relax_cell

        # --- 2. Head 检测逻辑 ---
        available_heads = getattr(self.calc, "available_heads", ["Default"])
        if available_heads is None: available_heads = ["Default"]
        
        target_heads_list = None
        if head is not None:
            if head not in available_heads:
                if head.lower() in available_heads:
                    head = head.lower()
                else:
                    raise ValueError(f"Selected head '{head}' not in {available_heads}")
            target_heads_list = [head]
            logger.info(f"Using manually selected head: {head}")
        else:
            if len(available_heads) == 1:
                target_heads_list = available_heads
            elif len(available_heads) > 1:
                raise ValueError(f"Multiple heads {available_heads} found. Please specify 'head=...'.")
            else:
                target_heads_list = ["Default"]
        
        # --- 3. 环境与日志设置 ---
        # 尝试获取 Rank ID 用于进度条显示
        try:
            rank = int(os.environ.get("SLURM_PROCID", os.environ.get("RANK", 0)))
        except:
            rank = 0

        log_level = logging.DEBUG if verbose else logging.INFO
        logger.setLevel(log_level)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
            logger.addHandler(handler)
        if save_log_file:
            file_handler = logging.FileHandler(save_log_file, mode='w')
            file_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
            logger.addHandler(file_handler)

        if not inplace:
            atoms_list = [at.copy() for at in atoms_list]
        
        queue = {i: at for i, at in enumerate(atoms_list)}
        relaxed_results = {}
        
        if trajectory_dir:
            os.makedirs(trajectory_dir, exist_ok=True)
            
        # 准备流式输出文件句柄
        stream_obj = None
        if append_trajectory_file:
            # 使用 extxyz 格式以保留 energy/forces 等信息
            stream_obj = open(append_trajectory_file, 'w')

        filter_cls = FrechetCellFilter if use_relax_cell else None

        # --- 4. 初始化 Worker ---
        worker = RelaxBatch(
            self.calc,
            optimizer_cls=self.optimizer_cls,
            fmax=fmax,
            atoms_filter_cls=filter_cls,
            max_n_steps=max_n_steps,
            device=self.device,
            optimizer_kwargs=optimizer_kwargs,
            target_heads=target_heads_list
        )

        # --- 5. 初始化进度条 (带 Rank 和 负载监控) ---
        # pbar = tqdm(
        #     total=len(atoms_list), 
        #     desc=f"[Rank {rank}] Relaxing", 
        #     unit="struct"
        # )
        
        # 引入 ase.io.write (确保已导入)
        from ase.io import write as ase_write

        try:
            while len(queue) > 0 or worker.num_active > 0:
                
                # --- A. 填充 (FILL) ---
                keys_to_remove = []
                for idx in list(queue.keys()):
                    # 检查显存负载
                    if worker.total_edges >= self.max_edges and worker.num_active > 0:
                        break
                    
                    atoms = queue[idx]
                    try:
                        # 预计算边数 (使用正确的 head)
                        data_obj = _get_mace_config_and_data(atoms, self.calc, heads=target_heads_list)
                        n_edges = data_obj.edge_index.shape[1]
                    except Exception as e:
                        logger.error(f"Failed to graph structure {idx}: {e}")
                        del queue[idx]
                        # pbar.update(1)
                        continue

                    # 再次检查单个结构是否会导致溢出
                    if n_edges > self.max_edges and worker.num_active > 0:
                        break
                    
                    # 决定是否生成调试用的过程轨迹
                    traj_path = None
                    if trajectory_dir:
                        name = atoms.info.get('name', atoms.info.get('ID', f"{idx}"))
                        traj_path = os.path.join(trajectory_dir, f"{name}.traj")
                    
                    worker.insert(atoms, n_edges, idx, logfile=None, traj_file=traj_path)
                    del queue[idx]
                
                # --- B. 计算 (COMPUTE) ---
                if worker.num_active > 0:
                    worker.step()
                
                # --- C. 清理 (PURGE) ---
                converged = worker.pop_converged()
                if converged:
                    for idx, atoms in converged:
                        relaxed_results[idx] = atoms
                        # pbar.update(1)
                        
                        # 流式写入最终结果
                        if stream_obj:
                            ase_write(stream_obj, atoms, format='extxyz')
                            stream_obj.flush() 
                
                # --- D. 更新监控信息 ---
                # pbar.set_postfix(
                #     active=worker.num_active, 
                #     edges=f"{worker.total_edges/1000:.1f}k"
                # )

        finally:
            # 确保关闭文件句柄
            if stream_obj:
                stream_obj.close()
            # pbar.close()

        logger.info(f"Relaxation finished.")
        # 按原始顺序返回结果
        return [relaxed_results.get(i, None) for i in range(len(atoms_list))]


def batch_calculate_properties(
    atoms_list: List[Atoms],
    model,
    batch_size: int = 32,
    device: str = "cuda",
    num_workers: int = 4,
    z_table=None,
    r_max=None,
    available_heads=None,
    calc=None,  # 新增：传入计算器以获取单位转换信息
):
    """批量并行计算单点能和力
    
    Args:
        atoms_list: ASE Atoms对象列表
        model: 已加载的MACE模型
        batch_size: 批处理大小
        device: 计算设备 (cuda/cpu)
        num_workers: DataLoader的工作进程数
        z_table: MACE计算器的z_table
        r_max: MACE计算器的r_max
        available_heads: MACE计算器的available_heads
        calc: MACE计算器对象（用于单位转换）
    """
    
    # 读取和转换数据
    if not isinstance(atoms_list, list):
        atoms_list = [atoms_list]
    
    if z_table is None or r_max is None or available_heads is None:
        raise ValueError("z_table, r_max, and available_heads must be provided")
    
    configs = [mace_data.config_from_atoms(s) for s in atoms_list]
    
    # 创建 DataLoader
    atomic_data_list = [
        mace_data.AtomicData.from_config(
            config,
            z_table=z_table,
            cutoff=r_max,
            heads=available_heads,
        )
        for config in configs
    ]
    
    # 限制num_workers以避免内存泄漏
    num_workers = min(num_workers, 2)  # 减少worker数
    
    loader = torch_geometric.dataloader.DataLoader(
        dataset=atomic_data_list,
        batch_size=batch_size,
        shuffle=False,
        drop_last=False,
        num_workers=num_workers,
        pin_memory=True,
    )
    
    # 批量计算
    results = {
        "energies": [],
        "forces": [],
        "stresses": [],
    }
    
    with torch.enable_grad():
        for batch in loader:
            batch = batch.to(device)
            batch["node_attrs"].requires_grad_(True)
            batch["positions"].requires_grad_(True)
            
            output = model(
                batch.to_dict(),
                compute_stress=True,
                training=False,
            )
            
            # 收集结果
            results["energies"].append(output["energy"].detach().cpu().numpy())
            
            # 按分子分割力（batch.ptr 指示每个分子的原子索引范围）
            forces_split = torch.split(output["forces"], 
                                      (batch.ptr[1:] - batch.ptr[:-1]).tolist())
            results["forces"].extend([f.detach().cpu().numpy() for f in forces_split])
            
            if "stress" in output:
                results["stresses"].append(output["stress"].detach().cpu().numpy())
            
            # 清理中间变量
            del batch, output
            torch.cuda.empty_cache() if device == "cuda" else None
    
    # 组合结果
    final_results = {
        "energies": np.concatenate(results["energies"]),
        "forces": results["forces"],
        "stresses": np.concatenate(results["stresses"]) if results["stresses"] else None,
    }
    
    # 清理内存
    del configs, atomic_data_list, loader, results
    import gc
    gc.collect()
    torch.cuda.empty_cache() if device == "cuda" else None
    
    return final_results

# 使用示例
if __name__ == "__main__":
    import time
    import ase
    from mace.calculators import MACECalculator
    
    # 1. 读取结构文件
    structures_file = r"d:\ECUST\Publication\Global_Optimization_Lib\SSW\ssw_output\ssw_structures.xyz"
    atoms_list = ase.io.read(structures_file, index=":")
    if not isinstance(atoms_list, list):
        atoms_list = [atoms_list]
    print(f"读取了 {len(atoms_list)} 个结构")
    
    # 2. 加载模型
    model_path = r"D:\ECUST\bin\VLMC-FeC-main\mace-omat-0-medium.model"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    calculator = MACECalculator(model_paths=model_path, device=device,enable_cueq=False, 
                dtype=torch.float32)
    model = calculator.models[0]
    model.eval()
    
    # 3. 并行计算
    start_time = time.time()
    results = batch_calculate_properties(
        atoms_list=atoms_list,
        model=model,
        batch_size=16,
        device=device,
        num_workers=0,
        z_table=calculator.z_table,
        r_max=calculator.r_max,
        available_heads=calculator.available_heads,
    )
    elapsed = time.time() - start_time
    
    print(f"计算了 {len(results['energies'])} 个结构")
    print(f"总耗时: {elapsed:.2f} 秒")
    print(f"平均每个结构: {elapsed/len(results['energies'])*1000:.2f} 毫秒")
    print(f"平均能量: {np.mean(results['energies']):.6f} eV")