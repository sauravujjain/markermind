# Surface Laptop 2 — Remote Nesting Worker (Ubuntu)

## Connection Details
- **IP**: 192.168.50.2 (direct ethernet to gaming PC at 192.168.50.1)
- **OS**: Ubuntu 24.04 (kernel 6.17), hostname `ubunu`
- **Username**: `ubunu`
- **SSH Key**: `~/.ssh/id_surface_new`
- **SSH config alias**: `ssh surface`

## Quick Connect (from gaming PC WSL2)
```bash
ssh surface
# Equivalent to:
ssh -i ~/.ssh/id_surface_new ubunu@192.168.50.2
```

## What's Installed
- Ubuntu 24.04 LTS (bare metal, not WSL)
- Python 3.12.3
- spyrrow 0.9.0 in `~/nesting_venv`
- numpy 2.4.4
- 8 CPU cores, 7.7 GB RAM

## Nesting Usage
```bash
# Activate venv and run nesting
ssh surface "source ~/nesting_venv/bin/activate && python3 -c '
import spyrrow
items = [spyrrow.Item(id=\"test\", shape=[(0,0),(100,0),(100,50),(0,50)], demand=2, allowed_orientations=[0,180])]
config = spyrrow.StripPackingConfig(total_computation_time=60)
instance = spyrrow.StripPackingInstance(name=\"marker\", strip_height=1500.0, items=items)
sol = instance.solve(config=config)
print(f\"Length: {sol.width:.1f}mm, density: {sol.density:.1%}\")
'"
```

## Spyrrow 0.9.0 API Quick Reference
```python
import spyrrow

# Create items
item = spyrrow.Item(
    id="piece_name",
    shape=[(x1,y1), (x2,y2), ...],  # polygon vertices
    demand=4,
    allowed_orientations=[0, 180]     # or None for free rotation
)

# Configure solver
config = spyrrow.StripPackingConfig(
    total_computation_time=120,  # seconds (80% explore, 20% compress)
    num_workers=None,            # None = use all cores
    min_items_separation=2.0,    # piece buffer in mm
)

# Create and solve instance
instance = spyrrow.StripPackingInstance(
    name="marker_name",
    strip_height=1500.0,  # fabric width in mm (NOTE: "height" = fixed dimension)
    items=[item1, item2, ...]
)
solution = instance.solve(config=config)

# Read results
print(solution.width)          # strip length (variable dimension)
print(solution.density)        # packing efficiency (0-1)
for pi in solution.placed_items:
    print(pi.id, pi.x, pi.y, pi.rotation)
```

## SSH Config (~/.ssh/config)
```
Host surface
    HostName 192.168.50.2
    User ubunu
    IdentityFile ~/.ssh/id_surface_new
    StrictHostKeyChecking no
    ConnectTimeout 5
```

## Long-Running Jobs (tmux)
```bash
# Start detached nesting job
ssh surface "tmux new-session -d -s nesting 'source ~/nesting_venv/bin/activate && python3 ~/nesting_job.py'"

# Check progress
ssh surface "tmux capture-pane -t nesting -p | tail -5"

# Retrieve results
scp surface:~/results.json ./
```

## If Connection Fails
1. Check cable: `ping 192.168.50.2`
2. Check SSH service: `ssh surface "sudo systemctl status ssh"`
3. Check IP config: `ssh surface "ip addr show"`

## Network Diagram
```
Gaming PC (WSL2)                    Surface Laptop 2 (Ubuntu)
┌─────────────────┐                ┌─────────────────┐
│  192.168.50.1    │◄──ethernet──►│  192.168.50.2    │
│  (Windows host)  │   1Gbps      │  (Ubuntu 24.04)  │
│                  │              │                  │
│  WSL2 bridges    │              │  SSH server      │
│  to host network │              │  Python 3.12     │
│                  │              │  spyrrow 0.9.0   │
└─────────────────┘                └─────────────────┘
     ssh surface ─────────────────► port 22
     scp files   ─────────────────► ~/
```
