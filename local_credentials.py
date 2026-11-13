"""Interactive password administration for the single local KIMTEK database."""
import argparse
import getpass
import json
import sqlite3
from contextlib import closing
from local_setup import TARGET,BASE


def set_password(cx,code,password):
    from core import hash_password,audit
    row=cx.execute('SELECT id FROM users WHERE employee_code=?',(code,)).fetchone()
    if not row:raise ValueError('Código local inexistente; utiliza --list.')
    cx.execute('UPDATE users SET password_hash=? WHERE id=?',(hash_password(password),row['id']))
    audit(cx,None,'CREDENCIAL_LOCAL','usuario',row['id'],'Restablecimiento local interactivo; contraseña no registrada')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--list',action='store_true');p.add_argument('--code')
    a=p.parse_args()
    if not TARGET.is_file() or TARGET.is_symlink() or not TARGET.resolve().is_relative_to(BASE.resolve()):p.error('Primero prepara la base local normal en su destino autorizado.')
    with closing(sqlite3.connect(TARGET)) as c:
        c.row_factory=sqlite3.Row
        if a.list:
            for r in c.execute('''SELECT u.employee_code,u.email,u.role,p.account_kind FROM users u
                JOIN local_staff_profiles p ON p.user_id=u.id ORDER BY p.account_kind DESC,u.role,u.employee_code'''):
                print(' | '.join(str(x) for x in r))
            return
        if not a.code:p.error('Usa --list o --code CODIGO. No se admiten contraseñas en argumentos.')
        first=getpass.getpass('Contraseña nueva (mínimo 10 caracteres): ')
        second=getpass.getpass('Repite la contraseña: ')
        if first!=second:raise ValueError('Las contraseñas no coinciden; no se cambió nada.')
        set_password(c,a.code,first);c.commit();print('Credencial actualizada. Ingresa por /login con el correo interno mostrado por --list.')


if __name__=='__main__':main()
