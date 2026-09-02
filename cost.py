import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 2, 100)

fixed_cost = 500       
variable_cost = 0.035  
pallet_value = 700     

total_cost = fixed_cost + (variable_cost * 3000) 
total_saving = pallet_value * x                  

# เปลี่ยนข้อความใน label และ title เป็นภาษาอังกฤษ
plt.plot(x, total_saving, label='Total Saving', color='green', linewidth=2)
plt.axhline(y=total_cost, color='red', linestyle='-', label='Total Cost (~605 THB)', linewidth=2)

plt.fill_between(x, total_cost, total_saving, where=(total_saving > total_cost), interpolate=True, color='lightgreen', alpha=0.3, label='Profit')
plt.fill_between(x, total_cost, total_saving, where=(total_saving <= total_cost), interpolate=True, color='lightcoral', alpha=0.3, label='Loss')

break_even_x = total_cost / pallet_value
plt.axvline(x=break_even_x, color='black', linestyle='--', label=f'Break-Even (~{break_even_x:.2f} Pallets)')

plt.title('Break-Even Analysis for Pallet AI Project')
plt.xlabel('Number of Saved Pallets (Per Month)')
plt.ylabel('Money (THB)')
plt.legend(loc='upper left')
plt.grid(True, linestyle=':', alpha=0.6)
plt.show()