"""Optional seed of the 9 units from the photo provided by the user.
Plate/name transcriptions should be verified with vehicle documents.
"""
from server import connect, init_db

UNITS = [
    ('PCP-1923','Winche','ESPECIAL'),
    ('PPB-2262','Cabezal','PESADO'),
    ('PFB-3999','Great Wall Wingle','LIVIANO'),
    ('PFG-2433','Kia Soluto','LIVIANO'),
    ('PDY-9427','Kia Picanto','LIVIANO'),
    ('PFU-3724','JAC 1035','PESADO'),
    ('PFT-9112','JAC 1040','PESADO'),
    ('PFM-2313','Chevrolet (modelo por confirmar)','POR_VERIFICAR'),
    ('PDX-2511','Chevrolet (modelo por confirmar)','POR_VERIFICAR'),
]

def seed():
    init_db()
    with connect() as cn:
        cn.execute('BEGIN IMMEDIATE')
        for plate,name,kind in UNITS:
            cn.execute('INSERT OR IGNORE INTO vehicles(plate,name,vehicle_type,notes) VALUES(?,?,?,?)',
                       (plate,name,kind,'Registro inicial de listado fotográfico; verificar datos.'))
        cn.execute('COMMIT')
    print('Flota inicial cargada (sin inventario ni registros de mantenimiento ficticios).')

if __name__=='__main__':seed()
