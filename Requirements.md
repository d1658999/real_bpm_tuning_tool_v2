## Requirements
### Source
- scikit-rf: https://scikit-rf.readthedocs.io/en/latest/

### 1 PM agent and 5 different style agents to fine tune impedance matching network. The agents will be assigned to different roles and work together to achieve the best impedance matching solution. Finally PM will judge which solution is best considering mass production tolerances (e.g. 5%) and the contour on Smith Chart is smallest and approach center.
1. Principal_engineer_Agent, whose role is "Product Manager".
    - Style: To judge which solution is best
    - Responsibility: To judge the greatest performance on impedance matching considering mass production tolerances (e.g. 5%) and the contour on Smith Chart is smallest and approach center

2.  Senior_engineer_Agent_1, whose role is "Senior RF engineer".
    - Style: use the fewest components among candidates whose non-antenna target error is within 10% of the best achievable non-antenna target error.
    - Responsibility: reject a poor zero-component/open result unless it is genuinely within the 10% near-optimal target-error set; break ties by total non-antenna plus antenna target error.
3.  Senior_engineer_Agent_2, whose role is "Senior RF engineer".
    - Style: balance low peak target mismatch and low insertion loss.
    - Responsibility: minimize `normalize(target_error_max) + normalize(worst_insertion_loss_magnitude)` across the complete candidate set.
4.  Senior_engineer_Agent_3, whose role is "Senior RF engineer".
    - Style: strictly minimize peak target mismatch across signal ports.
    - Responsibility: minimize the non-antenna target error first, then use the dependent antenna-port target error as the secondary objective.
5.  Senior_engineer_Agent_4, whose role is "Senior RF engineer".
    - Style: pursue the smallest target-centred contour region on the Smith Chart.
    - Responsibility: minimize the target-centred Smith score; the Rust sweep MAY use `target_error_spread + target_error_max` as the reference-compatible contour proxy.
6.  Senior_engineer_Agent_5, whose role is "Senior RF engineer".
    - Style: minimize insertion loss after meeting the active Smith target.
    - Responsibility: first keep candidates whose target error is no greater than `target_floor + max(0.005, 0.15 * target_floor)`, then select the lowest positive insertion-loss magnitude and use target error as the tie-breaker.

### Layout design
- There are three panels layout 
    - Right panel: 
      - Smith Chart(S11/S22): It shows the impedance matching network's performance across the frequency range. The Smith Chart should be presented like the format in the link [Smith Chart](https://scikit-rf.readthedocs.io/en/latest/tutorials/Plotting.html#Smith-Chart), please use right format to show the Smith Chart. 
      - Frequency response plots, which show the performance across the frequency range:
        - Insertion loss(S21)
        - VSWR(S11/S22)
        - Return loss(S11/S22)
      - Reset Original button: It will reset the Smith Chart and frequency response plots to the original state.
      - Zoom In/Out button: It will zoom in/out the Smith Chart and frequency response plots.
      - Move button: It will move the Smith Chart and frequency response plots.
      - Marker button: It will mark the point on the Smith Chart and frequency response plots if possible.
      - Save figures button: It will save the Smith Chart and frequency response plots to a file by .png and combine them together.

    - Middle panel: Connection setting for the every port of every snp file:
      - Default connection setting is `open` for every port, user can set the connection to `open`, `short`, `inductor`, `capacitor`, `inductor/capacitor`, `open/inductor/capacitor`, `connect`, `signal`. The tool will automatically check the connection setting and give a warning if the setting is invalid.
        - `short` means 0 ohm, so it does not count in the BOM count.
        - `open` means open circuit, so it does not count in the BOM count.
        - `inductor` means inductor, so it counts in the BOM count.
        - `capacitor` means capacitor, so it counts in the BOM count.
        - `inductor/capacitor` means inductor or capacitor.
        - `open/inductor/capacitor` means open or inductor or capacitor.
        - `connect` means connect to another port, so it does not count in the BOM count.
          - it can automatically show other snp files and let user select which snp and which port to connect to.
        - `signal` means signal port, so it does not count in the BOM count. User can assign it to s1/s2/s3/s4.
            - Maximum support 4 ports for signal, it means s1/s2/s3/s4, the lowest number port is for PAmid port, the higher number port is closer to antenna port. For example, if there are 2 ports assigned, s1 is for PAmid port, s2 is for antenna port. If there are 3 ports assigned, s1 is for PAmid port, s2 is for another PAmid port, s3 is for antenna port. If there are 4 ports assigned, s1 is for PAmid port, s2 is for another PAmid port, s3 is for the other PAmid port, s4 is for antenna port. The tool will automatically check the connection setting and give a warning if the setting is invalid.
      - The middle panel SHALL split the controls into two vertically arranged sections:
        - `Frequency and Smith targets` SHALL appear above `Port configuration`.
        - `Frequency and Smith targets` SHALL contain only `Start GHz`, `Stop GHz`, `Target`, `Target R Ω`, and `Target X Ω`. It SHALL NOT repeat the `File` or `Port` columns from `Port configuration`.
        - `Frequency and Smith targets` SHALL dynamically show only the driven signal rows (`s1`, `s2`, and `s3` when applicable). The highest assigned signal is the dependent antenna port and SHALL NOT appear in this section.
        - Each visible row SHALL be identified by its signal name so it remains clearly associated with the signal assignment shown in `Port configuration`.
        - The section SHALL automatically compact its table height to the header and visible driven-signal rows. All remaining vertical space SHALL be available to `Port configuration`.
      - Port configuration for Freq range: default is by the snp file, which can be set by user for specific frequency range to see the performance on Smith Chart and frequency response plots. 
      - If user has to point a specific point on Smith Chart as a target, user can set it and enable the feature, default is disabled. When enabled, the tool will try to find the best matching network to reach that point on Smith Chart. Please use non-normalized impedance to set the target point on Smith Chart, such as 50 ohm. 
      - If some snp files has more than 2 ports such as 3 ports or 4 ports, user can set freq for each port individually. For example, s1 is from 3.3GHz to 5GHz, s2 is from 1.4GHz to 2.7GHz, s3 don't care because it is dependent on s1 and s2 if the snp has 3 ports. If the snp has 4 ports, s1 is from 3.3GHz to 5GHz, s2 is from 1.4GHz to 2.7GHz, s3 is from 0.8GHz to 1.2GHz, s4 don't care because it is dependent on s1, s2 and s3. If ports only has s1 and s2, only s1 can be set, s2 is dependent on s1. what ports are assigned will be depentent on the signal port setting in the middle panel. 
    
    - Left panel: Add/Remove Selected multiple snp files from user uploaded or added
    
    - Top toolbar:
      - `Run Cascade` button: It will run the cascade of all snp files and show the performance on Smith Chart and frequency response plots. The tool will automatically check the connection setting and give a warning if the setting is invalid.
      - `Run Optimization` button: It will run the optimization of all snp files and show the performance on Smith Chart and frequency response plots. The tool will automatically check the connection setting and give a warning if the setting is invalid.
      - `Save Config` button: It will save the current configuration of all snp files and connection settings to a file by .json. 
      - `Load Config` button: It will load the configuration of all snp files and connection settings from a file by .json.
      - `Export SNP` button: It will export the present cascade snp files to a folder by .snp.
      - `Export IL CSV` button: It will export the present cascade insertion loss to a file by .csv.
    
### Real BOM 
- The components of real put in:
  - Capacitor: Murata GJM02 series at `Capacitors_BOM` folder
  - Inductor: Murata LQP02TQ series at `Inductors_BOM` folder
- When selecting those components, the tool can show their real value.

### Design style
- follow the design style in @DESIGN-apple.md

### Calculation and Algorithm
- If some calculations or algorithms are complex, it MUST use Rust not Python for performance optimization and speed up those complicated math calculation and then export to Python main coding. The tool will use Python for higher-level logic and integration and GUI.
- `Run optimization` shall use Rust to speed up the calculation and optimization.

#### Reference-style exhaustive optimizer
- The behavioral design source is `99_ reference/fleet_optimizer.py` and `99_ reference/lib.rs`. Production code SHALL implement the behavior in `bpm_tuner` and `rust_optimizer`; files under `99_ reference` SHALL remain unchanged reference material.
- Python SHALL build one base S-parameter network whose external ports are ordered as all signal ports first and all tunable ports afterward. The highest-numbered signal port SHALL be the dependent antenna/common port.
- Every disabled Smith target SHALL default to the Smith-chart center, equivalent to `50 + j0` ohm and reflection coefficient `target_gamma = 0`.
- The optimizer SHALL construct a Cartesian product of the allowed choices at every tunable port and evaluate every combination once:
  - `inductor`: sampled real Murata inductor choices when no fixed component is selected; an already selected part remains fixed.
  - `capacitor`: sampled real Murata capacitor choices when no fixed component is selected; an already selected part remains fixed.
  - `inductor/capacitor`: both real inductor and capacitor choices.
  - `open/inductor/capacitor`: an open baseline plus both real inductor and capacitor choices.
- `candidates_per_type` SHALL control how many evenly distributed real BOM parts of each requested type are included and SHALL default to `2`. The exhaustive combination count is the product of the candidate counts at all tunable ports. The former multi-pass coordinate-descent behavior SHALL NOT be used. Increasing this setting SHALL be presented as an exponential runtime and memory trade-off.
- Rust SHALL perform the expensive Cartesian sweep in parallel. For each tunable port `k`, it SHALL eliminate that port with the reference rank-one termination update:

  `S'[i,j] = S[i,j] + S[i,k] * gamma * S[k,j] / (1 - S[k,k] * gamma)`

- After each termination, port `k` is removed and the next tunable port shifts to the same index. After all terminations, only the ordered signal ports remain.
- The Rust sweep SHALL return, for every combination:
  - Maximum VSWR across all non-antenna signal ports, each evaluated in its configured frequency band.
  - Maximum VSWR at the dependent antenna port, evaluated over the union of the driven-signal bands.
  - Worst positive insertion-loss magnitude from each driven signal port to the antenna port.
  - `target_error_s11_max`: maximum `abs(Sii - target_gamma)` across non-antenna signal ports.
  - `target_error_s22_max`: maximum `abs(Saa - target_gamma)` at the dependent antenna port.
  - `target_error_max = max(target_error_s11_max, target_error_s22_max)`.
  - `target_error_spread = abs(target_error_s11_max - target_error_s22_max)`.
- All five agents SHALL select from the same complete sweep result. Their canonical result keys SHALL be `minimum_bom`, `balanced`, `minimum_target`, `smith_contour`, and `minimum_insertion_loss`.
- Candidate ordering and all tie-breakers SHALL be deterministic, with the candidate identifier used as the final tie-breaker.

#### Independent component tolerance sweep
- Each winning agent result SHALL be evaluated with independent component-value factors `[1.00, 0.95, 1.05]`. The tolerance study SHALL evaluate the Cartesian product of these factors across all selected components; it SHALL NOT vary every component together using only one global scale factor.
- Tolerance variation SHALL operate in the impedance domain using the nominal one-port reflection coefficient:

  `Z_nominal = Z0 * (1 + gamma_nominal) / (1 - gamma_nominal)`

  `Z_inductor_varied = Z_nominal * value_factor`

  `Z_capacitor_varied = Z_nominal / value_factor`

  `gamma_varied = (Z_varied - Z0) / (Z_varied + Z0)`

- The tolerance result SHALL record the worst VSWR, worst positive insertion-loss magnitude, worst target error, and `vswr_sensitivity = max(0, worst_vswr_5pct - nominal_worst_vswr)` across all independent variations.

### Show the progress of optimization
- Because the optimization may take a long time, the tool should show the progress of optimization in the GUI, such as a progress bar or a percentage. The tool should also allow user to cancel the optimization if needed.

### Multiple snp files
- It can allow user to add multiple snp files and run the cascade of all snp files without connecting every snp file necessarily. If user import or add multiple snp files but only use one of the snp files to set signal without connecting to other snp files, which behavior is allowed, but the condition is that the ports of other snp files will set them `open` for all ports. Because user may tentatively want to use the other snp files for future use or for reference, but not for the current optimization. 

### Smith Chart target point
- It can allow user to set a specific point on Smith Chart as a target, and the tool will try to find the best matching network to reach that point on Smith Chart by port configuration and frequency range setting. 
- For two ports, the target point is for s1 port, and s2 port is dependent on s1 port. For three ports, the target point is for s1 port and s2, and s3 port is dependent on s1 and s2 ports. For four ports, the target point is for s1 port, s2 port, and s3 port, and s4 port is dependent on s1, s2, and s3 ports.
- Individual ports has their own `Enable` checkbox to enable or disable the target point feature, default is disabled. 
- It is dynamic show the target point for ports. For example, it only two ports, just only show s1 port target point, s2 port is dependent on s1 port. If three ports, show s1 and s2 port target point, s3 port is dependent on s1 and s2 ports. If four ports, show s1, s2, and s3 port target point, s4 port is dependent on s1, s2, and s3 ports.

### Fleet requirements
- After `Run optimization`, the results from agents SHALL be saved to .json and .png files like example folder `outputs_port_target_optimization`. The result folder name is `Fleet_results_YYYYMMDD_HHMMSS` 
- Each agent JSON metrics object SHALL expose the prompt-facing names `target_error_max`, `target_error_5pct_max`, `worst_il_5pct_db`, `vswr_5pct_max`, and `risk_score`, in addition to any internal compatibility names.

#### Production risk score
- The production implementation SHALL calculate `risk_score` after all five reference-style agent winners and their independent tolerance sweeps are complete.
- A lower `risk_score` indicates a lower-risk result. The result with the lowest score SHALL be selected as the fleet winner.
- Each metric SHALL be min-max normalized across all completed agent results before its weight is applied:

  `normalize(x_i) = (x_i - min(x)) / (max(x) - min(x))`

- If every agent has the same value for a metric (`max(x) == min(x)`), the normalized value for that metric SHALL be `0.0` for every agent.
- The score SHALL use the following weights, which sum to `1.00`:

| Metric | Weight | Value used by the score |
| --- | ---: | --- |
| Worst target error under +/-5% component tolerance | 30% (`0.30`) | `target_error_5pct_max`; fall back to nominal `target_error_max` when the tolerance result is unavailable or zero |
| Component count | 25% (`0.25`) | `component_count` |
| VSWR sensitivity | 20% (`0.20`) | `vswr_sensitivity` |
| Worst insertion loss under +/-5% component tolerance | 15% (`0.15`) | Absolute value of `worst_il_5pct_db` |
| Target-error spread | 10% (`0.10`) | `target_error_spread`; fall back to `vswr_spread` when target-error spread is unavailable or zero |

- The required formula is:

  `risk_score = 0.30 * normalize(worst_target_error_5pct) + 0.25 * normalize(component_count) + 0.20 * normalize(vswr_sensitivity) + 0.15 * normalize(abs(worst_il_5pct_db)) + 0.10 * normalize(target_error_spread)`

- The final `risk_score` SHALL be rounded to four decimal places.
- The +/-5% component-tolerance evaluation is a conservative production-risk proxy; it SHALL NOT be presented as a measured yield, defect rate, or statistical process-capability result.


### Optimization time consumption
- The criteria for optimization time consumption is that the optimization time should be less than 10 minutes for 6 ports with 7 `candidates_per_type`. For example, if user has 6 ports and choose `Open/Inductor/Capacitor` and set `candidates_per_type` to 7, the combination count is (2*7+1)^6 = 15^6 = 11,390,625 combinations. The optimization time should be less than 10 minutes for this case. If the optimization time is more than 10 minutes, the tool should show a warning message to user and suggest user to reduce the `candidates_per_type` or reduce the number of ports.
- The maximum combination count is less than 0.1 billion because the optimization time will be too long and the memory consumption will be too high. If the combination count is more than 0.1 billion, the tool should show a warning message to user and suggest user to reduce the `candidates_per_type` or reduce the number of ports.