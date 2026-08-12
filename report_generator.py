import os
import datetime
import matplotlib
matplotlib.use('Agg')  # Non-interactive background renderer for Docker/headless environments
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
from csv_logger import get_monthly_billing_data
from logger import log

def generate_monthly_report_image(period: str = "last_month") -> str:
    """
    Generates a modern high-resolution PNG infographic report for the specified month.
    Plots daily usage date vs variable grid energy cost (EXCLUDING fixed daily fees) and solar generation.
    Returns the absolute filepath to the saved PNG image.
    """
    data = get_monthly_billing_data(period=period)
    if "error" in data:
        log.error(f"Failed to generate report data: {data['error']}")
        return ""

    month_label = data["month_label"]
    daily_records = data["daily_records"]
    
    output_dir = "logs/monthly_bills"
    os.makedirs(output_dir, exist_ok=True)
    
    # Create filename key (e.g. monthly_report_2026_07.png)
    file_tag = data["start_date"][:7].replace("-", "_")
    output_path = os.path.abspath(os.path.join(output_dir, f"monthly_report_{file_tag}.png"))

    # Set up dark aesthetic styling
    plt.style.use('dark_background')
    fig = plt.figure(figsize=(12, 10), dpi=150)
    fig.patch.set_facecolor('#0F172A')  # Slate dark background

    gs = gridspec.GridSpec(3, 1, height_ratios=[1.2, 3.5, 1.3], hspace=0.35)

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 1: Header & Executive Metric Cards
    # ──────────────────────────────────────────────────────────────────────────
    ax_header = fig.add_subplot(gs[0])
    ax_header.axis('off')
    ax_header.set_facecolor('#0F172A')

    # Main Title
    ax_header.text(0.5, 0.88, f"⚡ MONTHLY ELECTRICITY & ENERGY REPORT", color='#F8FAFC', fontsize=18, fontweight='bold', ha='center')
    ax_header.text(0.5, 0.72, f"{month_label.upper()}  •  {data['utility_rate_plan']}", color='#94A3B8', fontsize=11, ha='center')

    # Metric Cards (4 Boxes)
    cards = [
        {"title": "NET UTILITY BILL", "value": f"${data['estimated_net_bill_dollars']:.2f}", "color": "#38BDF8"},
        {"title": "SELF-POWERED", "value": f"{data['self_powered_percentage']}%", "color": "#10B981"},
        {"title": "SOLAR GENERATED", "value": f"{data['total_solar_kwh']} kWh", "color": "#F59E0B"},
        {"title": "EV CHARGING COST", "value": f"${data['ev_charging_cost_dollars']:.2f}", "color": "#A855F7"},
    ]

    card_positions = [0.03, 0.28, 0.53, 0.78]
    card_width = 0.19

    for i, c in enumerate(cards):
        x = card_positions[i]
        # Box background
        rect = mpatches.FancyBboxPatch((x, 0.1), card_width, 0.52, transform=ax_header.transAxes,
                                      facecolor='#1E293B', edgecolor='#334155', linewidth=1.5, boxstyle="round,pad=0.03")
        ax_header.add_patch(rect)
        # Text
        ax_header.text(x + card_width/2, 0.48, c["title"], transform=ax_header.transAxes, color='#94A3B8', fontsize=8, fontweight='bold', ha='center')
        ax_header.text(x + card_width/2, 0.22, c["value"], transform=ax_header.transAxes, color=c["color"], fontsize=15, fontweight='bold', ha='center')

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 2: Daily Energy Usage & Variable Cost Chart
    # ──────────────────────────────────────────────────────────────────────────
    ax_chart = fig.add_subplot(gs[1])
    ax_chart.set_facecolor('#1E293B')

    if daily_records:
        days_labels = [r["date_short"] for r in daily_records]
        grid_costs = [r["variable_grid_cost"] for r in daily_records] # Variable cost EXCLUDES fixed fee!
        solar_kwhs = [r["solar_kwh"] for r in daily_records]
        x_indices = list(range(len(daily_records)))

        # Plot 1: Daily Variable Grid Cost ($) as Bar Chart
        bars = ax_chart.bar(x_indices, grid_costs, color='#F59E0B', alpha=0.85, width=0.55, label='Grid Electricity Cost ($) [Excl. Fixed Fee]')

        ax_chart.set_ylabel('Daily Variable Grid Cost ($)', color='#F59E0B', fontsize=11, fontweight='bold')
        ax_chart.tick_params(axis='y', labelcolor='#F59E0B')
        ax_chart.set_xticks(x_indices)
        ax_chart.set_xticklabels(days_labels, rotation=45, ha='right', fontsize=8, color='#CBD5E1')

        # Plot 2: Daily Solar Generation (kWh) on Twin Axis Line
        ax_solar = ax_chart.twinx()
        line = ax_solar.plot(x_indices, solar_kwhs, color='#38BDF8', linewidth=2.5, marker='o', markersize=4, label='Solar Generation (kWh)')
        ax_solar.set_ylabel('Solar Generation (kWh)', color='#38BDF8', fontsize=11, fontweight='bold')
        ax_solar.tick_params(axis='y', labelcolor='#38BDF8')
        ax_solar.grid(False)

        # Title & Grid lines
        ax_chart.set_title("Daily Energy Usage Date vs. Variable Grid Cost & Solar Generation", color='#F8FAFC', fontsize=12, fontweight='bold', pad=12)
        ax_chart.grid(True, linestyle='--', alpha=0.15, color='#94A3B8')

        # Combined Legend
        lines_1, labels_1 = ax_chart.get_legend_handles_labels()
        lines_2, labels_2 = ax_solar.get_legend_handles_labels()
        ax_chart.legend(lines_1 + lines_2, labels_1 + labels_2, loc='upper left', facecolor='#0F172A', edgecolor='#334155', fontsize=9)
    else:
        ax_chart.text(0.5, 0.5, "No daily log data recorded for this month yet.", color='#94A3B8', fontsize=12, ha='center', va='center')

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 3: Detailed Billing Breakdown Footer
    # ──────────────────────────────────────────────────────────────────────────
    ax_footer = fig.add_subplot(gs[2])
    ax_footer.axis('off')
    ax_footer.set_facecolor('#0F172A')

    # Footer Box
    rect_footer = mpatches.FancyBboxPatch((0.02, 0.05), 0.96, 0.90, transform=ax_footer.transAxes,
                                         facecolor='#1E293B', edgecolor='#334155', linewidth=1.5, boxstyle="round,pad=0.03")
    ax_footer.add_patch(rect_footer)

    col1_text = (
        f"• Total Home Consumption: {data['total_home_kwh']} kWh\n"
        f"• Total Grid Imported: {data['total_grid_import_kwh']} kWh\n"
        f"• Total Solar Exported: {data['total_solar_export_kwh']} kWh"
    )

    col2_text = (
        f"• Appliance Grid Energy Cost: ${data['home_appliances_cost_dollars']:.2f}\n"
        f"• EV Charging Grid Cost: ${data['ev_charging_cost_dollars']:.2f}\n"
        f"• Solar Export Credit: -${data['total_solar_export_credit_dollars']:.2f}"
    )

    col3_text = (
        f"• Variable Grid Energy: ${data['total_variable_grid_cost_dollars']:.2f}\n"
        f"• Fixed Service Fee ({data['days_count']}d): +${data['fixed_service_fee_dollars']:.2f}\n"
        f"• ESTIMATED NET BILL: ${data['estimated_net_bill_dollars']:.2f}"
    )

    ax_footer.text(0.05, 0.5, col1_text, transform=ax_footer.transAxes, color='#CBD5E1', fontsize=9.5, va='center', multialignment='left')
    ax_footer.text(0.38, 0.5, col2_text, transform=ax_footer.transAxes, color='#CBD5E1', fontsize=9.5, va='center', multialignment='left')
    ax_footer.text(0.70, 0.5, col3_text, transform=ax_footer.transAxes, color='#F8FAFC', fontsize=9.5, fontweight='bold', va='center', multialignment='left')

    plt.tight_layout()
    fig.savefig(output_path, facecolor=fig.get_facecolor(), edgecolor='none', bbox_inches='tight')
    plt.close(fig)

    log.info(f"Monthly report image generated successfully: {output_path}")
    return output_path
