import logging
import os
import aiogram.utils.markdown as md
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Text
from aiogram.types.message import ContentType
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import ParseMode
import app.store.parus.models as parus
import app.store.local.models as local
from tools.helpers import split_fio, temp_file_path, echo_error, keys_exists
from tools.cp1251 import decode_cp1251
from app.settings import config_token, config


# Команды бота
BOT_COMMANDS = '''start - получение табеля из Паруса
group - выбор другой группы
org - выбор другого учреждения
cancel - отмена текущей команды
reset - отмена авторизации в Парусе
ping - проверка отклика бота
help - что может делать этот бот?'''

# Уровень логов
logging.basicConfig(level=logging.INFO)

# Aiogram Telegram Bot
bot = Bot(token=config_token['token'])
dp = Dispatcher(bot, storage=MemoryStorage())


# Состояния конечного автомата
class Form(StatesGroup):
    inn = State()    # ввод ИНН учреждения
    fio = State()    # ввод ФИО сотрудника
    group = State()  # ввод группы учреждения


async def receive_timesheet(message: types.Message, state: FSMContext):
    """
    Получение табеля посещаемости из Паруса
    """
    user = await local.get_user(state, message.from_user.id)
    if keys_exists(['db_key', 'org_rn', 'group'], user):
        try:
            # Получение табеля посещаемости из Паруса в файл CSV во временную директорию
            file_path = await parus.receive_timesheet(user['db_key'], user['org_rn'], user['group'])
            # Отправка табеля посещаемости пользователю
            if os.path.exists(file_path):
                with open(file_path, 'rb') as file:
                    org = await local.get_org(state, message.from_user.id)
                    org_info = f'Учреждение: {org["org_name"]}\n' if keys_exists(['org_name'], org) else None
                    await message.reply_document(
                        file,
                        caption=f'{org_info}Группа: {user["group"]}',
                        reply_markup=types.ReplyKeyboardRemove())
                # Удаление файла из временной директории
                os.remove(file_path)
                # Увеличение счётчика получения
                await local.inc_receive_count(state, message.from_user.id)
        except Exception as error:
            await echo_error(message, f'Ошибка получения табеля посещаемости из Паруса:\n{error}')
    else:
        # Авторизация и повторное получение табеля
        await cmd_start(message, state)
    # Завершение команды
    await state.finish()


async def send_timesheet(message: types.Message, state: FSMContext, file_path):
    """
    Отправка табеля посещаемости в Парус
    """
    try:
        org = await local.get_org(state, message.from_user.id)
        if keys_exists(['db_key', 'company_rn', 'org_inn'], org):
            # Отправка табеля
            send_result = await parus.send_timesheet(org['db_key'], org['company_rn'], file_path)
            os.remove(file_path)
            await message.reply(send_result, reply_markup=types.ReplyKeyboardRemove())
            await state.finish()
            # Увеличение счётчика отправки
            await local.inc_send_count(state, message.from_user.id)
            return True
    except Exception as error:
        await echo_error(message, f'Ошибка отправки табеля посещаемости в Парус: {error}')
    return False


@dp.message_handler(commands='start')
async def cmd_start(message: types.Message, state: FSMContext):
    """
    Авторизация и отправка или получение табеля посещаемости из Паруса
    """
    user = await local.get_user(state, message.from_user.id)
    if keys_exists(['org_rn', 'person_rn', 'group'], user):
        # Получение табеля посещаемости из Паруса
        await receive_timesheet(message, state)
    elif not keys_exists(['org_rn'], user):
        # Обработка ИНН, если пользователь не найден
        await prompt_to_input_inn(message)
        await Form.inn.set()
    elif not keys_exists(['person_rn'], user):
        # Обработка ФИО, если его нет
        await prompt_to_input_fio(message)
        await Form.fio.set()
    elif not keys_exists(['group'], user):
        # Обработка группы, если её нет
        await prompt_to_input_group(message, state)
        await Form.group.set()


@dp.message_handler(state='*', commands='cancel')
@dp.message_handler(Text(equals='cancel', ignore_case=True), state='*')
async def cancel_handler(message: types.Message, state: FSMContext):
    """
    Отмена текущей команды
    """
    await message.reply('Команда отменена', reply_markup=types.ReplyKeyboardRemove())
    await state.finish()


@dp.message_handler(lambda message: not (message.text.isdigit() and len(message.text) == 10), state=Form.inn)
async def process_inn_invalid(message: types.Message):
    """
    Проверка ИНН
    """
    return await message.reply("ИНН должен содержать 10 цифр")


@dp.message_handler(state=Form.inn)
async def process_inn(message: types.Message, state: FSMContext):
    """
    Обработка ИНН
    """
    org_inn = message.text
    # Поиск учреждения в MongoDB по ИНН
    org = await local.get_org_by_inn(org_inn)
    if not keys_exists(['org_name', 'company_name'], org):
        # Поиск базы данных Паруса и учреждения в ней по ИНН
        org = await parus.get_org_by_inn(org_inn)
        if org:
            # Учреждение найдено
            await local.insert_org(state, org)
        else:
            # Учреждение не найдено
            await message.reply(f'Учреждение с ИНН {org_inn} не подключено к сервису.\n'
                                f"Обратитесь к разработчику {config['developer']['telegram']}")
            await state.finish()
            return
    # Вывод информации об учреждении
    await message.reply(f'Учреждение: {org["org_name"]}\nОрганизация: {org["company_name"]}')
    # Создание пользователя с привязкой к учреждению
    await local.create_user(state, message, org)
    # Обработка ФИО
    await prompt_to_input_fio(message)
    await Form.fio.set()


@dp.message_handler(state=Form.fio)
async def process_fio(message: types.Message, state: FSMContext):
    """
    Обработка ФИО
    """
    fio = message.text
    family, firstname, lastname = split_fio(fio)
    user = await local.get_user(state, message.from_user.id)
    if not keys_exists(['db_key', 'org_rn'], user):
        # Авторизация
        await cmd_start(message, state)
        return
    # Поиск сотрудника учреждения по ФИО в Парусе
    try:
        person_rn = await parus.find_person_in_org(user['db_key'], user['org_rn'], family, firstname, lastname)
        # Сотрудник учреждения не найден
        if not person_rn:
            # Сотрудник не найден в Парусе
            await message.reply(f'Сотрудник {fio} в учреждении не найден.\n'
                                f"Обратитесь к разработчику {config['developer']['telegram']}")
            await state.finish()
            return
    except Exception as error:
        await echo_error(message, f'Ошибка поиска сотрудника в Парусе: {error}')
        await state.finish()
        return
    # Сохранение реквизитов сотрудника учреждения
    user.update({'person_rn': person_rn, 'family': family, 'firstname': firstname, 'lastname': lastname})
    await local.update_user(state, user)
    # Проверка наличия файла с табелем во временной директории
    data = await state.get_data()
    if keys_exists(['file_path'], data):
        file_path = data['file_path']
        if os.path.exists(file_path):
            await send_timesheet(message, state, file_path)
            del data['file_path']
            await state.set_data(data)
    else:
        # Обработка группы
        await prompt_to_input_group(message, state)
        await Form.group.set()


@dp.message_handler(state=Form.group)
async def process_group(message: types.Message, state: FSMContext):
    """
    Обработка группы
    """
    user = await local.get_user(state, message.from_user.id)
    if user:
        # Сохранение группы
        group = message.text
        user['group'] = group
        await local.update_user(state, user)
        # Получение табеля посещаемости из Паруса
        await receive_timesheet(message, state)
    else:
        # Авторизация
        await cmd_start(message, state)


@dp.message_handler(commands='group')
async def cmd_group(message: types.Message, state: FSMContext):
    """
    Выбор другой группы
    """
    # Удаление группы
    user = await local.get_user(state, message.from_user.id)
    if keys_exists(['group'], user):
        del user['group']
        await local.update_user(state, user)
    # Обработка другой группы
    await prompt_to_input_group(message, state)
    await Form.group.set()


@dp.message_handler(commands='org')
async def cmd_org(message: types.Message, state: FSMContext):
    """
    Авторизация другого учреждения
    """
    # Удаление пользователя
    await local.delete_user(state, message.from_user.id)
    # Обработка другого ИНН
    await cmd_start(message, state)


@dp.message_handler(commands='reset')
async def cmd_reset(message: types.Message, state: FSMContext):
    """
    Удаление авторизации
    """
    await local.delete_user(state, message.from_user.id)
    await message.reply('Авторизация в Парусе отменена', reply_markup=types.ReplyKeyboardRemove())


@dp.message_handler(commands='ping')
async def cmd_ping(message: types.Message):
    """
    Проверка отклика бота
    """
    await message.reply('pong')


@dp.message_handler(commands='help')
async def cmd_help(message: types.Message):
    """
    Что может делать этот бот?
    """
    def format_command(command_line):
        command, desc = [x.strip() for x in command_line.split('-')]
        return md.text(md.link(f'/{command}', f'/{command}'), f' - {desc}')

    commands = [format_command(cl) for cl in BOT_COMMANDS.splitlines()]
    await message.reply(
        md.text(
            md.text(
                'Получение и отправка табелей из мобильного приложения ',
                md.link('Табели посещаемости', 'https://github.com/parusinf/timesheets'),
                ' в систему управления ',
                md.link('Парус', 'https://parus.com/'),
            ),
            md.text(md.bold('\nКоманды')),
            *commands,
            md.text('\nДля отправки табеля в Парус отправьте его боту из мобильного приложения\n'),
            md.text(md.bold('Разработчик')),
            md.text(f"{config['developer']['name']} {config['developer']['telegram']}"),
            sep='\n',
        ),
        reply_markup=types.ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN,
    )


@dp.message_handler(content_types=ContentType.DOCUMENT)
async def process_timesheet(message: types.Message, state: FSMContext):
    """
    Отправка табеля посещаемости в Парус
    """
    # От пользователя получен файл с табелем посещаемости
    if message.document:
        file_name = message.document['file_name']
        file_path = temp_file_path(file_name)
        file_ext = os.path.splitext(file_name)[1]
        if '.csv' == file_ext:
            try:
                # Загрузка файла от пользователя во временную директорию
                await message.document.download(destination_file=file_path)
            except Exception as error:
                await echo_error(message, f'Ошибка загрузки файла с табелем посещаемости: {error}')
            # Проверка авторизации учреждения и пользователя
            org_inn_from_file = get_org_inn_from_file(file_path)
            org = await local.get_org(state, message.from_user.id)
            user = await local.get_user(state, message.from_user.id)
            if keys_exists(['org_inn'], org) and org_inn_from_file == org['org_inn'] \
                    and keys_exists(['person_rn'], user):
                # Отправка табеля посещаемости в Парус
                if await send_timesheet(message, state, file_path):
                    return
            # Сохранение пути файла для загрузки после авторизации
            await state.update_data({'file_path': file_path})
            # Удаление авторизации в другом учреждении
            if keys_exists(['org_inn'], org):
                await local.delete_user(state, message.from_user.id)
            # Авторизация в учреждении с ИНН в табеле
            message.text = org_inn_from_file
            await process_inn(message, state)
        else:
            await echo_error(message, 'Файл не содержит табель посещаемости')


def get_org_inn_from_file(file_path):
    """
    Получение ИНН учреждения из файла табеля
    """
    with open(file_path, 'rb') as file:
        file_content = decode_cp1251(file.read())
    file_lines = file_content.splitlines()
    org_fields = file_lines[1].split(';') if len(file_lines) >= 2 else None
    return org_fields[1] if len(org_fields) >= 2 else None


async def prompt_to_input_inn(message: types.Message):
    """
    Приглашение к вводу ИНН учреждения
    """
    await message.reply("ИНН вашего учреждения?")


async def prompt_to_input_fio(message: types.Message):
    """
    Приглашение к вводу ФИО сотрудника учреждения
    """
    await message.reply('Ваши Фамилия Имя Отчество?')


async def prompt_to_input_group(message: types.Message, state: FSMContext):
    """
    Приглашение к выбору групп учреждения
    """
    user = await local.get_user(state, message.from_user.id)
    # Получение списка групп учреждения
    if keys_exists(['db_key', 'org_rn'], user):
        try:
            groups = await parus.get_groups(user['db_key'], user['org_rn'])
            # Действующие группы в учреждении не найдены
            if not groups:
                raise AttributeError('Действующие группы в учреждении не найдены')
            # Приглашение к выбору группы учреждения
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, selective=True)
            markup.add(*groups.split(';'))
            await message.reply('Выберите группу', reply_markup=markup)
        except Exception as error:
            await echo_error(message, f'Ошибка получения списка групп из Паруса: {error}')
            await state.finish()
    else:
        # Авторизация
        await cmd_start(message, state)