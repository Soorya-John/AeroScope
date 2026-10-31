"""One-time source migration, kept as a reproducible maintenance utility."""
from pathlib import Path
p=Path('frontend/static/app.js')
s=p.read_text(encoding='utf-8')
s=s.replace('<option value="82" ${m.command==82?\'selected\':\'\'}>Spline waypoint</option>', '<option value="82" ${m.command==82?\'selected\':\'\'}>Spline waypoint</option>'+''.join(f'<option value="{n}" ${{m.command=={n}?\'selected\':\'\'}}>{label}</option>' for n,label in [(20,'Return to launch'),(178,'Change speed'),(206,'Camera trigger distance')]))
s=s.replace('<select class="mission-value" data-field="frame">','<select class="mission-value" data-field="frame"><option value="2" ${m.frame==2?\'selected\':\'\'}>Mission action (no altitude)</option>')
s=s.replace('if(![5,6].includes(Number(m.frame??6)))','if(![2,5,6].includes(Number(m.frame??6)))')
p.write_text(s,encoding='utf-8')
