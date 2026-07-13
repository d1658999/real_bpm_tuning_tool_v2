## Requirements
### Source
- scikit-rf: https://scikit-rf.readthedocs.io/en/latest/

### 1 PM agent and 5 different style agents to fine tune impedance matching network. The agents will be assigned to different roles and work together to achieve the best impedance matching solution. Finally PM will judge which solution is best considering mass production tolerances (e.g. 5%) and the contour on Smith Chart is smallest and approach center.
1. Principal_engineer_Agent, whose role is "Product Manager".
    - Style: To judge which solution is best
    - Responsibility: To judge the greatest performance on impedance matching considering mass production tolerances (e.g. 5%) and the contour on Smith Chart is smallest and approach center

2.  Senior_engineer_Agent_1, whose role is "Senior RF engineer".
    - Style: achieve an acceptable impedance match (e.g., VSWR < 1.4 or lower) using the fewest possible components, rejecting "no component" if performance is poor
    - Responsibility: find the solution with the minimum BOM count that still meets a baseline performance requirement (VSWR < 1.4 or lower) if possible
3.  Senior_engineer_Agent_2, whose role is "Senior RF engineer".
    - Style: pursue the optimal performance for balance to complete the whole impedance matching
    - Responsibility: research to balance low VSWR and low insertion loss
4.  Senior_engineer_Agent_3, whose role is "Senior RF engineer".
    - Style: pursue the lowest VSWR to complete the whole impedance matching
    - Responsibility: research to strictly minimize VSWR across all frequencies, even if it means using more components or accepting higher insertion loss  
5.  Senior_engineer_Agent_4, whose role is "Senior RF engineer".
    - Style: pursue the smallest contour region and approach center on Smith Chart
    - Responsibility: research to minimize the contour area or trace on Smith Chart
6.  Senior_engineer_Agent_5, whose role is "Senior RF engineer".
    - Style: pursue the lowest insertion loss |S21|
    - Responsibility: research the lowest insertion loss regardless of minor VSWR peaks

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