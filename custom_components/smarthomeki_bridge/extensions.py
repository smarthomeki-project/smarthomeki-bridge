"""Bounded read APIs and approval-gated calendar/device extensions."""
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import math

DOMAIN = "smarthomeki_bridge"
CAL = 'calendar.smarthomeki_events'
DEFAULTS = dict(import_price=.30, export_price=.08, monthly_fixed=0, weekly=True, monthly=True, report_time='18:00')
ENERGY = {k:'sensor.smarthomeki_total_'+v for k,v in {'import':'energy_import','export':'energy_export','load':'load_consumption','pv':'production','charge':'battery_charge','discharge':'battery_discharge'}.items()}

def schema(props, required):
    return dict(type='object',properties=props,required=required,additionalProperties=False)
S={'type':'string'}; N={'type':'number'}
TOOLS=[]
def tool(name, desc, props, required, read=True):
    TOOLS.append(dict(name=name,description=desc,inputSchema=schema(props,required),annotations={'readOnlyHint':read}))
tool('get_smarthomeki_history','Read numeric sensor history, max 7 days, 2000 points.',{'entity_id':S,'start':S,'end':S},['entity_id','start','end'])
tool('get_smarthomeki_statistics','Read hourly/day/month recorder statistics for numeric sensors, max 366 days.',{'entity_id':S,'start':S,'end':S,'period':{'enum':['hour','day','month'],'type':'string'}},['entity_id','start','end','period'])
tool('get_smarthomeki_energy','Energy totals and variable costs from recorder sums; dates YYYY-MM-DD, end exclusive.',{'start':S,'end':S},['start','end'])
tool('get_smarthomeki_calendar','Read family calendar, max 366 days.',{'start':S,'end':S},['start','end'])
tool('get_smarthomeki_settings','Read energy/report settings.',{},[])
for suffix,read in [('preview',True),('propose',False)]:
    desc='Validate only.' if read else 'Requires explicit chat approval before staging and separate HA button press within 120 seconds.'
    tool(suffix+'_smarthomeki_calendar_event',desc+' Create one family calendar event; end exclusive for all-day dates.',{'summary':S,'start':S,'end':S},['summary','start','end'],read)
    tool(suffix+'_smarthomeki_device',desc+' Allowlisted climate temperature or vacuum start/pause/return.',{'entity_id':S,'service':S,'temperature':N},['entity_id','service'],read)
    tool(suffix+'_smarthomeki_settings',desc+' Replace tariff/report settings.',{'import_price':N,'export_price':N,'monthly_fixed':N,'weekly':{'type':'boolean'},'monthly':{'type':'boolean'},'report_time':S},list(DEFAULTS),read)

def dt(hass, s):
    if not isinstance(s,str): raise ValueError('Expected ISO date/time')
    d=datetime.fromisoformat(s)
    if d.tzinfo is None: d=d.replace(tzinfo=ZoneInfo(hass.config.time_zone))
    return d

def interval(hass,a,days):
    s,e=dt(hass,a['start']),dt(hass,a['end'])
    if not timedelta(0)<e-s<=timedelta(days=days): raise ValueError('Invalid interval')
    return s,e

def sensor(hass,e):
    if not isinstance(e,str) or not e.startswith('sensor.'): raise ValueError('Only numeric sensors')
    st=hass.states.get(e)
    if st is None or not st.attributes.get('unit_of_measurement'): raise ValueError('Sensor not eligible')
    return e

async def statistics(hass,ids,s,e,period):
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
    return await get_instance(hass).async_add_executor_job(statistics_during_period,hass,s,e,set(ids),period,None,{'sum','mean','min','max','state'})

async def events(hass,s,e):
    if not hass.services.has_service('calendar','get_events'): raise ValueError('Calendar service unavailable')
    r=await hass.services.async_call('calendar','get_events',{'entity_id':hass.data[DOMAIN]['calendar'],'start_date_time':s.isoformat(),'end_date_time':e.isoformat()},blocking=True,return_response=True)
    return r.get(hass.data[DOMAIN]['calendar'],{}).get('events',[])

async def validate(hass,kind,a):
    if kind=='calendar_event':
        s,e=interval(hass,a,366)
        if not isinstance(a['summary'],str) or not 0<len(a['summary'])<=200: raise ValueError('Invalid summary')
        if (len(a['start'])==10)!=(len(a['end'])==10): raise ValueError('Mixed date types')
        st=hass.states.get(hass.data[DOMAIN]['calendar'])
        if st is None or st.state=='unavailable' or not hass.services.has_service('calendar','create_event'): raise ValueError('Family calendar unavailable')
        for event in await events(hass,s,e):
            if event.get('summary')==a['summary']: raise ValueError('Possible duplicate event; inspect calendar')
    elif kind=='device':
        entity=a['entity_id']; service=a['service']; st=hass.states.get(entity)
        if st is None or st.state in ('unknown','unavailable'): raise ValueError('Device unavailable')
        domain=entity.split('.')[0]
        if domain=='climate' and service=='set_temperature':
            t=a.get('temperature'); lo=max(16,st.attributes.get('min_temp',16)); hi=min(26,st.attributes.get('max_temp',26))
            if type(t) not in (int,float) or not lo<=t<=hi: raise ValueError('Temperature outside allowed range')
        elif domain=='vacuum' and service in ('start','pause','return_to_base') and 'temperature' not in a: pass
        else: raise ValueError('Action forbidden')
        if not hass.services.has_service(domain,service): raise ValueError('Service unavailable')
    elif kind=='settings':
        for k in ('import_price','export_price','monthly_fixed'):
            if type(a[k]) not in (int,float) or not math.isfinite(a[k]) or not 0<=a[k]<= (1000 if k=='monthly_fixed' else 5): raise ValueError('Invalid tariff')
        if any(type(a[k]) is not bool for k in ('weekly','monthly')): raise ValueError('Invalid report flag')
        if len(a['report_time'])!=5: raise ValueError('Use HH:MM')
        datetime.strptime(a['report_time'],'%H:%M')
    else: raise ValueError('Unknown extension')

async def apply(hass,state,p):
    kind=p['extension']; a=p['args']; await validate(hass,kind,a)
    if kind=='settings':
        await state['settings_store'].async_save(a); state['settings']=a.copy()
    elif kind=='calendar_event':
        data={'entity_id':hass.data[DOMAIN]['calendar'],'summary':a['summary']}
        for k in ('start','end'): data[k+('_date' if len(a[k])==10 else '_date_time')]=a[k] if len(a[k])==10 else dt(hass,a[k]).isoformat()
        await hass.services.async_call('calendar','create_event',data,blocking=True)
    else:
        data={'entity_id':a['entity_id']}
        if 'temperature' in a: data['temperature']=a['temperature']
        await hass.services.async_call(a['entity_id'].split('.')[0],a['service'],data,blocking=True)

def _daily_total(rows, boundaries):
    boundaries = set(boundaries)
    sums = {}
    for row in rows:
        end = row.get("end")
        if end not in boundaries:
            continue
        value = row.get("sum")
        if end in sums or type(value) not in (int, float) or not math.isfinite(value):
            return None
        sums[end] = value
    if sums.keys() != boundaries:
        return None
    ordered = [sums[end] for end in sorted(boundaries)]
    if any(current < previous for previous, current in zip(ordered, ordered[1:])):
        return None
    return round(ordered[-1] - ordered[0], 3)


async def energy(hass, state, s, e):
    # Recorder daily sums represent the end of each local calendar day.
    boundaries = [
        (s + timedelta(days=offset)).timestamp()
        for offset in range((e.date() - s.date()).days + 1)
    ]
    entities = state.get("energy_sensors", ENERGY)
    rows = await statistics(hass, list(entities.values()), s - timedelta(days=1), e, "day")
    totals = {
        key: _daily_total(rows.get(entity, []), boundaries)
        for key, entity in entities.items()
    }
    settings=state['settings']; imp=totals['import']; exp=totals['export']
    return {'kwh':totals,'import_cost':None if imp is None else round(imp*settings['import_price'],2),'export_revenue':None if exp is None else round(exp*settings['export_price'],2),'note':'Daily recorder sum differences; missing buckets return null. Fixed costs excluded; tariff is current, not historical.'}

async def dispatch(hass,state,name,a,stage):
    spec=next(t for t in TOOLS if t['name']==name)['inputSchema']
    if not isinstance(a,dict) or set(a)-set(spec['properties']) or set(spec['required'])-set(a): raise ValueError('Invalid arguments')
    if name=='get_smarthomeki_settings': return state['settings']
    if name=='get_smarthomeki_calendar': return await events(hass,*interval(hass,a,366))
    if name=='get_smarthomeki_energy':
        if any(len(a[k])!=10 for k in ('start','end')): raise ValueError('Use dates')
        return await energy(hass,state,*interval(hass,a,366))
    if name=='get_smarthomeki_statistics':
        e=sensor(hass,a['entity_id']); s,t=interval(hass,a,366)
        if a['period'] not in ('hour','day','month') or (a['period']=='hour' and t-s>timedelta(days=30)): raise ValueError('Invalid period')
        return await statistics(hass,[e],s,t,a['period'])
    if name=='get_smarthomeki_history':
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import get_significant_states
        e=sensor(hass,a['entity_id']); s,t=interval(hass,a,7)
        r=await get_instance(hass).async_add_executor_job(get_significant_states,hass,s,t,[e])
        rows=r.get(e,[]); out=[]
        for x in rows[:2000]:
            try: v=float(x.state)
            except (ValueError,TypeError): continue
            if math.isfinite(v): out.append({'time':x.last_updated.isoformat(),'value':v})
        return {'points':out,'truncated':len(rows)>2000}
    kind=name.split('_smarthomeki_',1)[1]; await validate(hass,kind,a)
    if name.startswith('preview'): return {'valid':True,'saved':False,'args':a}
    async with state['lock']:
        pid=stage(state,hass,{'kind':'extension','extension':kind,'args':a.copy()})
    await hass.services.async_call('persistent_notification','create',{'title':'SmartHomeKI Freigabe','message':f'{pid}: {kind} {a}. Nur bei Zustimmung innerhalb 120 Sekunden Freigabetaster drücken.'},blocking=False)
    return {'proposal_id':pid,'status':'pending','saved':False,'expires_in_seconds':120}

def setup_reports(hass,state):
    from homeassistant.helpers.event import async_track_time_change
    async def report(now):
        cfg=state['settings']; local=now.astimezone(ZoneInfo(hass.config.time_zone))
        if local.strftime('%H:%M')!=cfg['report_time']: return
        today=local.date()
        for kind,enabled,start,end in [('Woche',cfg['weekly'] and today.weekday()==6,today-timedelta(days=7),today),('Monat',cfg['monthly'] and today.day==1,(today-timedelta(days=1)).replace(day=1),today)]:
            if not enabled: continue
            try:
                result=await energy(hass,state,dt(hass,start.isoformat()),dt(hass,end.isoformat()))
                msg=f'{start} bis {end} (exklusiv): {result}'
            except Exception as exc: msg='Bericht nicht verfügbar: '+type(exc).__name__
            await hass.services.async_call('persistent_notification','create',{'title':'SmartHomeKI '+kind,'message':msg,'notification_id':'smarthomeki_report_'+kind},blocking=False)
    async_track_time_change(hass,report,second=0)
