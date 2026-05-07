# ABB Robot Imaging Platform Research

This note records the current design consensus for using an ABB six-axis robot as a programmable imaging platform.

The goal is to let experiment code stay in Python or Rust while the ABB controller remains responsible for safe robot motion.

## Target Use Case

The platform mounts imaging hardware on a six-axis ABB robot:

- camera
- lens
- light source
- optional trigger, IO, or illumination controller

The robot provides repeatable camera and light positioning. The PC-side application provides experiment scheduling, camera control, light control, metadata capture, and data storage.

Typical experiments should look like high-level application code rather than robot-controller code:

```python
for pose in scan_poses:
    robot.move_l(pose)
    robot.wait_done()

    light.set_channel("brightfield", intensity=60)
    camera.capture(f"{sample_id}_{pose.name}_brightfield.tiff")

    light.set_channel("darkfield", intensity=80)
    camera.capture(f"{sample_id}_{pose.name}_darkfield.tiff")
```

## ABB Control Boundary

ABB robots commonly expose motion through a controller-side RAPID program. RAPID is ABB's proprietary robot language; it is not the same thing as a communication protocol.

For this project, RAPID should be treated as a thin robot-side adapter, not as the place where experiment logic lives.

```text
Python / Rust experiment application
  experiment recipes, scan planning, camera SDK, light control, storage
        |
        | TCP/socket or another selected transport
        v
ABB RAPID adapter
  parse simple commands, execute MoveJ/MoveL/SetDO, return status
        |
        v
ABB controller motion kernel
```

The intended developer experience is that most code is written on the PC side. The RAPID layer exists so the ABB controller can receive simple external commands and translate them into safe native motion instructions.

## RAPID Adapter Role

The RAPID adapter should be intentionally small and stable.

Responsibilities:

- accept commands from the external PC
- validate command shape and motion parameters
- execute basic robot primitives such as `MoveJ`, `MoveL`, and digital IO changes
- expose current status, errors, and completion acknowledgements
- keep ABB-specific details out of the experiment application

Non-responsibilities:

- experiment scheduling
- image acquisition logic
- camera SDK integration
- light-source sequencing beyond simple IO or trigger commands
- sample metadata and data storage

This keeps the proprietary ABB layer shallow enough that it can be generated, reviewed, and rarely touched.

## EGM Decision

ABB option `689-1 Externally Guided Motion` appears to correspond to EGM-style externally guided motion.

EGM is useful when the external computer must provide high-frequency online guidance, such as visual servoing, continuous tracking, or custom servo-loop behavior.

For the imaging platform described here, EGM is not required for the first version. Basic imaging experiments usually need repeatable point-to-point or linear moves, then a settled capture at each pose. A socket/RAPID adapter is enough for that class of work.

Use EGM later only if the platform needs closed-loop online motion while imaging.

## PC Requirement

The platform needs an external PC or industrial computer.

The ABB controller should not be treated as the general application computer. The external PC runs:

- Python or Rust experiment orchestration
- AutoWeaver workflow/runtime code
- camera SDKs
- light-controller communication
- robot adapter client
- data logging and storage
- optional local UI or web UI

## PC Hardware Direction

A Raspberry Pi can send robot commands and handle light IO for simple setups, but it is not the best default for an imaging platform.

Recommended baseline:

- small x86 PC or industrial computer
- Ubuntu LTS or another stable Linux distribution
- 16 GB RAM minimum
- NVMe storage, preferably 512 GB or larger
- enough USB3, GigE, or 10GigE bandwidth for the selected camera
- ideally two Ethernet ports if robot and camera networks should be separated

Raspberry Pi is acceptable only for lightweight control, low data volume, and simple camera requirements. Industrial camera SDK support and high-throughput capture are usually better on x86 Linux.

Windows remains a viable fallback if a required camera SDK or vendor toolchain has better Windows support. The preferred default is Linux because service management, SSH access, logs, automation, and long-running experiment control are simpler.

## System Architecture

```text
AutoWeaver / experiment application on Linux PC
  - workflow and recipe execution
  - scan pose generation
  - camera acquisition
  - light sequencing
  - metadata and data storage
  - robot command client

Robot transport
  - TCP/socket protocol, RWS, or another selected ABB-compatible transport

ABB controller
  - RAPID adapter
  - motion execution
  - safety state and robot IO

Physical station
  - six-axis robot
  - camera/lens/light payload
  - sample fixture
  - safety enclosure or operating boundary
```

## Key Engineering Risks

### Calibration

The project needs a clear calibration story for robot tool center point, camera coordinate frame, sample coordinate frame, and any fixture coordinate system.

Without this, the system may move repeatably but not produce reproducible imaging geometry.

### Trigger Synchronization

For high repeatability, image capture should prefer hardware trigger paths where possible.

Python sleeps are acceptable for early tests but should not be the long-term synchronization mechanism for precise imaging.

### Cable Management

Camera cables, light cables, network cables, and trigger wires must be routed for six-axis motion. Cable strain and collision risk should be designed before relying on the station for repeatable experiments.

### Safety

ABB industrial robots are not inherently collaborative. The platform should assume proper safety boundaries, emergency stop behavior, reduced speed during development, collision avoidance, and fixture clearance checks.

### Metadata Discipline

Every captured image should record enough context to reproduce the experiment:

- robot pose
- tool and work object identifiers
- camera exposure and gain
- lens and magnification settings where available
- light channel and intensity
- sample identifier
- timestamp
- software recipe version

## Initial Implementation Direction

The first implementation should favor a simple, inspectable protocol between the PC and ABB RAPID adapter.

Candidate command set:

- `PING`
- `HOME`
- `MOVEJ`
- `MOVEL`
- `SETDO`
- `GETPOSE`
- `STATUS`
- `STOP`

The PC-side API should hide transport and ABB-specific details behind a small robot client:

```python
robot.home()
robot.move_j(joints, speed="slow")
robot.move_l(pose, speed="scan")
robot.set_output("camera_trigger", True)
robot.wait_done(timeout=10.0)
```

This client can later become an AutoWeaver communication adapter or side task, but the first priority is to validate the robot-imaging workflow end to end.

## Current Consensus

- Use ABB for the robot platform if stability, payload, and repeatability matter more than easiest SDK onboarding.
- Keep RAPID as a thin adapter and keep experiment intelligence on the PC.
- Do not require EGM for the first version of the imaging platform.
- Prefer a small x86 Linux PC over Raspberry Pi for camera-heavy work.
- Build the system around high-level experiment recipes, not around hand-written robot programs.
- Treat calibration, triggering, cable routing, and metadata as first-class parts of the platform design.
