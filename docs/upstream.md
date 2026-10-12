# Связь с исходным UNetLab

Основа выделения: коммит `d647a2d` исходного проекта (ветка `work`). Ветка
`puskalka` содержит серверный исполнитель для одной попытки Moodle.

| Исходный компонент | Роль в пускалке |
| --- | --- |
| `html/includes/__lab.php` | Чтение `.unl`, нод, сетей, интерфейсов, embedded configs: `model.py` |
| `html/templates/*.php` | QEMU-профили экспортированы в `profiles/*.json` |
| `html/includes/__node.php` | Модели NIC, MAC и контроллеров дисков: `qemu.py` |
| `html/includes/cli.php` | Разностные диски, TAP/bridges и boot media: `storage.py`, `network.py`, `bootstrap.py` |
| `wrappers/unl_wrapper.php` | Порядок сборки всей лаборатории: сначала сети, затем ноды |
| `wrappers/qemu_wrapper.c` | Надзор за QEMU заменён отслеживаемыми процессами и QMP в `processes.py` |
| `scripts/minidisk.bz2` | Исходный FAT-носитель vIOS сохранён в `assets/` |
| `patch/linux-lts-utopic-3.16.0.patch` | Историческое требование прозрачных L2-сегментов; справочный материал для ядра бати |
| `labs/Featured/Cisco/Basic/STP.unl` | Исходный пример сохранён в `examples/upstream-stp.unl` |

UI, Slim/PHP API, авторизация, SQLite/users/pods, редактор, локальные desktop
wrappers, IOL/Dynamips/VPCS, старая упаковка Debian/OVA и управление Docker-нодами
удалены из этой ветки. Они доступны через историю Git. Docker здесь служит
оболочкой всей QEMU-лаборатории, поэтому управление отдельными Docker-нодами не
требуется в первой итерации.

Сохраняются аппаратные параметры старых QEMU-профилей. Это не гарантия
совместимости каждого старого гостевого образа с текущим QEMU: исходник
использовал несколько старых версий. Менеджер может выбрать согласованный
профиль/аргументы и предоставить нужный бинарник в производном образе.

Для служебных L2-кадров исходник зависел от патча ядра. Пускалка проверяет эту
возможность вместо незаметного отключения STP/LLDP/LACP. OVS-профили, serial
связи между нодами и vendor-specific pexpect/disk-modification workflows
пока отклоняются явно; их нельзя считать уже перенесёнными.

Код и данные, основанные на UNetLab, сохраняют copyright Andrea Dainese и
BSD-3-Clause лицензию из корневого `LICENSE`.
