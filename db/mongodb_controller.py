from typing import Optional

from requests import post
from db import query_builder
from src.typing import Data

import functools
from datetime import datetime
from bson.objectid import ObjectId

from pymongo.database import Database
from pymongo.results import InsertOneResult, UpdateResult

from db.query_builder import QueryBuilder
from src.models import TICKLIST, TickListProblem
from src.config import *

USERS_COLLECTION = 'users'


def preprocess_boulder_data(boulder):
    # inverse maps
    for field in FIELDS_TO_MAP.keys():
        if field in boulder:
            inv_map = {v: k for k, v in FIELDS_TO_MAP[field].items()}
            boulder[field] = inv_map[boulder[field]]
    return boulder


def postprocess_boulder_data(func):
    """
    Postprocess the data returned by the DB and add/delete
    missing fields. This decorator is used to make sure that
    the data returned by the DB is consistent and contains
    the expected fields.
    It acts as an anti curruption layer to keep models up to
    date if any changes have been made to the models.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        boulder_data = func(*args, **kwargs)
        # Add/delete required boulder fields
        # handle 3 possible types:
        #   1. list
        #   2. dict containing multiple objects
        #   3. single object as dict
        fields_to_check = {
            'repetitions': 0
        }
        maps_to_apply = FIELDS_TO_MAP
        if isinstance(boulder_data, list):
            for boulder in boulder_data:
                # skip if boulder data is empty for some reason
                if not boulder:
                    continue
                for field in fields_to_check:
                    if field in boulder:
                        continue
                    boulder[field] = fields_to_check[field]
                for field in maps_to_apply:
                    if field in boulder:
                        boulder[field] = maps_to_apply[field][boulder[field]]
        elif isinstance(boulder_data, dict) and boulder_data:
            # check if Items is key
            if ITEMS in boulder_data:
                for boulder in boulder_data[ITEMS]:
                    # skip if boulder data is empty for some reason
                    if not boulder:
                        continue
                    for field in fields_to_check:
                        if field in boulder:
                            continue
                        boulder[field] = fields_to_check[field]
                    for field in maps_to_apply:
                        if field in boulder:
                            boulder[field] = maps_to_apply[field][boulder[field]]
            else:
                # skip if boulder data is empty for some reason
                if boulder_data:
                    for field in fields_to_check:
                        if field in boulder_data:
                            continue
                        boulder_data[field] = fields_to_check[field]
                    for field in maps_to_apply:
                        if field in boulder_data:
                            boulder_data[field] = maps_to_apply[field][boulder_data[field]]
        return boulder_data
    return wrapper


def serializable(func):
    """
    Make sure that the value returned by a function
    is serializable. The main problem is that objects
    retrieved from the DDBB have an _id key whose value
    is an ObjectId(), which is not serializable.
    Another option would be to use json_util:
    https://pymongo.readthedocs.io/en/stable/api/bson/json_util.html
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        value = func(*args, **kwargs)
        if type(value) is dict and '_id' in value:
            return make_object_serializable(value)
        elif type(value) == dict:
            for key, val in value.items():
                if type(val) == list:
                    value[key] = make_list_serializable(val)
                elif '_id' in val:
                    value[key] = make_object_serializable(val)
            return value
        elif type(value) == list:
            return make_list_serializable(value)
        else:
            if type(value) == ObjectId:
                return str(value)
            return value
    return wrapper


def make_object_serializable(element: Data) -> Data:
    """
    Make sure an element can be serialized
    """
    if not element:
        return {}
    if '_id' in element:
        element['_id'] = str(element['_id'])
    return element


def make_list_serializable(data: list[Data]) -> list[Data]:
    """
    Given a list, make sure all of its
    elements are serializable
    """
    if not data:
        return []
    for element in data:
        make_object_serializable(element)
    return data


@serializable
def get_gyms(database: Database) -> list[Data]:
    """
    Get the list of available gyms
    """
    return list(database['walls'].find())


@serializable
def get_gym_walls(gym: str, database: Database, latest: bool = False) -> list[Data]:
    """
    Return the list of available walls for a specific
    Gym
    """
    query_builder = QueryBuilder()
    if latest:
        query_builder.equal('latest', True)
    return list(database[f'{gym}_walls'].find(query_builder.query))


def get_gym_pretty_name(gym: str, database: Database) -> str:
    """
    Get the actual Gym name from its path

    IF the gym cannot be found, return an empty string
    """
    data = database['walls'].find_one(
        {'id': gym}, {'name': 1})  # move this cases to query builder?
    return data.get('name', '') if data else ''


def get_wall_name(gym_name: str, wall_section: str, database: Database) -> str:
    """
    Get the actual wall name from its path as a string

    If the wall cannot be found, return an empty string
    """
    data = database[f'{gym_name}_walls'].find_one(
        {'image': wall_section}, {'name': 1})
    return data.get('name', '') if data else ''


def get_gym_section_name(gym: str, section, database: Database) -> str:
    """
    Given a gym and a section image filename, return the
    proper name of the section as a string
    """
    return get_wall_name(gym, section, database)


def get_walls_radius_all(database: Database) -> dict[str, float]:
    """
    Get the list of all radius used to paint the
    circles in the different wall sections:
    {
        'sancu/s1': 0.0317124736,
        'sancu/s2': 0.0317124736,
        'sancu/s3': 0.0317124736,
        'sancu/s4': 0.0317124736,
        [...]
    }
    """
    gym_ids = [gym['id'] for gym in get_gyms(database)]
    walls_with_radius = {}
    for gym in gym_ids:
        gym_walls_list = get_gym_walls(gym, database)
        walls_with_radius = {
            **walls_with_radius,
            **{f"{gym}/{wall['image']}": wall['radius'] for wall in gym_walls_list}
        }
    return walls_with_radius


@serializable
@postprocess_boulder_data
def get_circuits(gym: str, database: Database) -> dict[str, list[Data]]:
    """
    Get the whole list of circuits for the specified gym

    The returned dictionary has one key-value pair.
    The key is 'Items' and the value is a list of raw boulder data.
    """
    raw_circuit_data = list(database[f'{gym}_circuits'].find())
    return {ITEMS: raw_circuit_data}


@serializable
@postprocess_boulder_data
def get_boulders(gym: str, database: Database) -> dict[str, list[Data]]:
    """
    Get the whole list of boulders for the specified gym

    The returned dictionary has one key-value pair.
    The key is 'Items' and the value is a list of raw boulder data.
    """
    raw_boulder_data = list(database[f'{gym}_boulders'].find())
    return {ITEMS: raw_boulder_data}


@serializable
def get_routes(gym: str, database: Database) -> dict[str, list[Data]]:
    """
    Get the whole list of routes for the specified gym

    The returned dictionary has one key-value pair.
    The key is 'Items' and the value is a list of raw route data.
    """
    raw_route_data = list(database[f'{gym}_routes'].find())
    return {ITEMS: raw_route_data}


@serializable
def put_boulder(boulder_data: Data, gym: str, database: Database) -> InsertOneResult:
    """
    Store a new boulder for the specified gym
    """
    result = database[f'{gym}_boulders'].insert_one(
        preprocess_boulder_data(boulder_data))
    if result is not None:
        return result.inserted_id


@serializable
def put_circuit(circuit_data: Data, gym: str, database: Database) -> InsertOneResult:
    """
    Store a new circuit for the specified gym
    """
    result = database[f'{gym}_circuits'].insert_one(
        preprocess_boulder_data(circuit_data))
    if result is not None:
        return result.inserted_id


@serializable
def put_route(route_data: Data, gym: str, database: Database) -> InsertOneResult:
    """
    Store a new route for the specified gym
    """
    result = database[f'{gym}_routes'].insert_one(route_data)
    if result is not None:
        return result.inserted_id


@serializable
def put_boulder_in_ticklist(boulder_data: Data, user_id: str, database: Database, mark_as_done_clicked: bool = False) -> list[Data]:
    """
    Store a new boulder in the user's ticklist, change its
    is_done status or add a new climbed date

    Return the updated ticklist
    """
    IS_DONE = 'is_done'
    IDEN = 'iden'
    DATE_CLIMBED = 'date_climbed'
    # TICKLIST = 'ticklist'
    # USERS = 'users'
    # user = database[USERS].find_one({'id': user_id})
    user = database[USERS_COLLECTION].find_one(
        QueryBuilder().equal('id', user_id).query)
    # get ticklist
    ticklist = user.get(TICKLIST, [])
    # check if problem is already in the user's ticklist
    boulder = list(filter(lambda x: x[IDEN] == boulder_data[IDEN], ticklist))
    # Boulder is not in ticklist
    if not boulder:
        # Add it to ticklist, either marked as done or not
        if boulder_data[IS_DONE] and mark_as_done_clicked:
            boulder_data[DATE_CLIMBED] = [
                datetime.today().strftime('%Y-%m-%d')]
        ticklist.append(boulder_data)
        update_user_ticklist(database, ticklist, user, user_id)
    # boulder is already in ticklist and marked as done
    elif boulder and mark_as_done_clicked and boulder_data[IS_DONE]:
        # find boulder index in ticklist
        index = find_boulder_index(boulder_data, ticklist)
        # mark boulder as done
        ticklist[index][IS_DONE] = boulder_data[IS_DONE]
        # Set climbed date. If string, change to list
        ticklist = set_climbed_date(ticklist, index)
        update_user_ticklist(database, ticklist, user, user_id)
    return ticklist


def update_user_ticklist(database: Database, ticklist: list[Data], user: Data, user_id: str) -> None:
    """
    Update a user's ticklist, both DDBB and in memory projections
    """
    user[TICKLIST] = ticklist
    database[USERS_COLLECTION].update_one(
        QueryBuilder().equal('id', user_id).query, {'$set': user})


def find_boulder_index(boulder_data: Data, boulders: list[Data]) -> int:
    """
    Given a list of boulders and the data from a single boulder,
    find the boulder in the list and return its index if found.
    Else return -1
    """
    IDEN = 'iden'
    for index, t_boulder in enumerate(boulders):
        if t_boulder[IDEN] == boulder_data[IDEN]:
            return index
    return -1


@serializable
def set_climbed_date(ticklist: list[Data], index: int, climbed_date: Optional[datetime] = None) -> list[Data]:
    """
    Given a list of boulders and an index, update the climbed
    date of the boulder at the given index

    Return the updated ticklist
    """
    DATE_CLIMBED = 'date_climbed'
    if not climbed_date:
        climbed_date = datetime.today()
    # backwards compatibility, where we were storing date_climbed as a string
    if type(ticklist[index].get(DATE_CLIMBED, None)) == str:
        # Convert to list and add new date
        ticklist[index][DATE_CLIMBED] = [
            ticklist[index].get(DATE_CLIMBED),
            climbed_date.strftime('%Y-%m-%d')
        ]
    # If it is already a list, add new date
    elif type(ticklist[index].get(DATE_CLIMBED, None)) == list:
        ticklist[index][DATE_CLIMBED] += [climbed_date.strftime('%Y-%m-%d')]
    # date climbed does not exist yet
    else:
        ticklist[index][DATE_CLIMBED] = [climbed_date.strftime('%Y-%m-%d')]
    return ticklist


@serializable
def delete_boulder_in_ticklist(boulder_data: Data, user_id: str, database: Database) -> list[Data]:
    """
    Delete the selected problem from the user's ticklist

    Return the filtered list of boulders with the given one removed
    """
    user = database[USERS_COLLECTION].find_one(
        QueryBuilder().equal('id', user_id).query)
    filtered_list = []
    if user:
        # get ticklist
        ticklist = user.get('ticklist', [])
        # remove problem from list
        filtered_list = list(
            filter(lambda x: x['iden'] != boulder_data['iden'], ticklist))
        user['ticklist'] = filtered_list
        database[USERS_COLLECTION].update_one(
            QueryBuilder().equal('id', user_id).query, {'$set': user})

    return filtered_list


@serializable
@postprocess_boulder_data
def get_user_problem_list_by_id(user_id: str, list_id: str, database: Database) -> list:
    problem_list = database[USERS_COLLECTION].find_one(
        QueryBuilder().equal('id', user_id).query, {list_id: 1})
    return problem_list.get(list_id, []) if problem_list else []


@serializable
@postprocess_boulder_data
def get_ticklist_boulder(boulder: TickListProblem, database: Database) -> Data:
    """
    Given a ticklist problem, get the remaining problem fields

    Return a boulder data with 'gym', 'is_done', and 'date_climbed' fields
    """
    boulder_data = database[f'{boulder.gym}_boulders'].find_one(
        boulder.iden)
    if not boulder_data:
        boulder_data = database[f'{boulder.gym}_boulders'].find_one(
            ObjectId(boulder.iden))
    if not boulder_data:
        return {}

    boulder_data['gym'] = boulder.gym
    boulder_data['is_done'] = boulder.is_done
    # backwards compatibility
    if boulder.date_climbed:
        boulder_data['date_climbed'] = boulder.date_climbed if type(
            boulder.date_climbed) == list else [boulder.date_climbed]
    else:
        boulder_data['date_climbed'] = []
    return boulder_data


@serializable
@postprocess_boulder_data
def get_boulder_by_name(gym: str, name: str, database: Database) -> Data:
    """
    Given a boulder name and a Gym, return the boulder data
    Return an empty dictionary if the boulder is not found
    """
    boulder = database[f'{gym}_boulders'].find_one(
        QueryBuilder().equal('name', name).query)

    return boulder if boulder else {}


@serializable
@postprocess_boulder_data
def get_boulder_by_id(gym: str, boulder_id: str, database: Database) -> Data:
    """
    Given a boulder id and a Gym, return the boulder data
    Return an empty dictionary if the boulder is not found
    """
    boulder = database[f'{gym}_boulders'].find_one(
        QueryBuilder().equal('_id', ObjectId(boulder_id)).query
    )
    return boulder if boulder else {}

@serializable
@postprocess_boulder_data
def get_circuit_by_name(gym: str, name: str, database: Database) -> Data:
    """
    Given a circuit name and a Gym, return the circuit data
    Return an empty dictionary if the circuit is not found
    """
    circuit = database[f'{gym}_circuits'].find_one(
        QueryBuilder().equal('name', name).query)

    return circuit if circuit else {}


@serializable
@postprocess_boulder_data
def get_circuit_by_id(gym: str, circuit_id: str, database: Database) -> Data:
    """
    Given a circuit id and a Gym, return the boulder data
    Return an empty dictionary if the circuit is not found
    """
    circuit = database[f'{gym}_circuits'].find_one(
        QueryBuilder().equal('_id', ObjectId(circuit_id)).query
    )
    return circuit if circuit else {}

@serializable
@postprocess_boulder_data
def get_random_boulder(gym: str, database: Database) -> Data:
    """Given a gym code, return a random boulder from it

    :param gym: gym from which to get the random boulder
    :type gym: str
    :param database: database to use
    :type database: Database
    :return: boulder data
    :rtype: Data
    """
    boulder = None
    try:
        boulder = database[f'{gym}_boulders'].aggregate(
            [{'$sample': {'size': 1}}]).next()
    except StopIteration:
        boulder = None
    return boulder if boulder else {}


@serializable
@postprocess_boulder_data
def get_next_boulder(
        boulder_id: str,
        gym: str,
        user_id: str,
        latest_wall_set: bool,
        sort_by: str,
        is_ascending: bool,
        to_show: str,
        database: Database) -> Data:
    """
    Given a boulder id, get the next boulder based on insertion date

    :param boulder_id: boulder ID for which to get next boulder
    :type boulder_id: str
    :param gym: gym code of the boulders through which to iterate
    :type gym: str
    :param database: database connection
    :type database: Database
    :return: next boulder if there is any, empty dict otherwise
    :rtype: Data
    """
    # TODO: query can be reworked so that all happens in the DDBB and not all
    # problems have to be retrieved
    # build the query
    SORTING_FIELD_MAP = {
        'creation_date': '_id',  # insertion order is by date
        'difficulty': 'difficulty',
        'section': 'section',
        'rating': 'rating',
        # Here we might have problems if not all boulders have repetitions
        'repetitions': 'repetitions'
    }
    sorting_field = SORTING_FIELD_MAP[sort_by]

    query_builder = QueryBuilder()

    if latest_wall_set:
        walls = get_gym_walls(gym, database, latest_wall_set)
        query_builder.contained_in(
            'section', [wall['image'] for wall in walls])

    boulders = list(
        database[f'{gym}_boulders'].find(query_builder.query).sort([
            (sorting_field, 1 if is_ascending else -1),
            ('time', -1)
        ])
    )

    # if show only to do, remove problems present as done in user ticklist
    if to_show == 'to_do' and user_id:
        done_boulders = [b['iden'] for b in get_user_problem_list_by_id(
            user_id, 'ticklist', database) if b['is_done'] == True]
        boulders = [boulder for boulder in boulders if str(
            boulder['_id']) not in done_boulders]
        # [(b['name'], b['difficulty'], b['time']) for b in sorted(a, key=lambda x: (-x['difficulty'], -(datetime.datetime.strptime(x['time'], '%Y-%m-%dT%H:%M:%S.%f') - datetime.datetime(1, 1, 1)).total_seconds()))]

    next_boulder = {}
    if boulders:
        idx = [str(b['_id']) for b in boulders].index(boulder_id)
        if idx < len(boulders) - 1:
            next_boulder = boulders[idx+1]
        else:
            next_boulder = boulders[idx]
    return next_boulder


@serializable
@postprocess_boulder_data
def get_next_boulder_from_user_list(boulder_id, list_id, user_id, latest_wall_set, sort_by, is_ascending, to_show, database):
    SORTING_FIELD_MAP = {
        'creation_date': '_id',  # insertion order is by date
        'difficulty': 'difficulty_int',
        'section': 'section',
        'rating': 'rating',
        # Here we might have problems if not all boulders have repetitions
        'repetitions': 'repetitions'
    }
    REVERSE_MAPS = {
        'green': 0,
        'blue': 1,
        'yellow': 2,
        'red': 3
    }

    # What a pain to have to recover all boulders...
    ticklist_p = get_user_problem_list_by_id(user_id, list_id, database)
    problems = [get_boulder_by_id(b['gym'], b['iden'], database)
                for b in ticklist_p]
    # match fields
    for p in problems:
        for t in ticklist_p:
            if p['_id'] == t['iden']:
                p['gym'] = t['gym']
                p['is_done'] = t['is_done']
                p['difficulty_int'] = REVERSE_MAPS[p['difficulty']]

    # Apply sorting and filtering criteria
    if to_show == 'done':
        problems = [p for p in problems if p['is_done']]
    elif to_show == 'to_do':
        problems = [p for p in problems if not p['is_done']]
    # sorted_problem_list = sorted(problems, key=lambda p: (p[SORTING_FIELD_MAP[sort_by]], (-1 if not is_ascending else 1) *datetime.timestamp(datetime.fromisoformat(p['time']))), reverse=not is_ascending)
    problems.sort(key=lambda p: (p[SORTING_FIELD_MAP[sort_by]], (1 if not is_ascending else -1)
                  * datetime.timestamp(datetime.fromisoformat(p['time']))), reverse=not is_ascending)

    next_boulder = {}

    # print([(b['name'],b[SORTING_FIELD_MAP[sort_by]],datetime.timestamp(datetime.fromisoformat(b['time']))) for b in problems])

    idx = -1
    if problems:
        # wrap in try catch ? if not found we can keep showing the current boulder
        idx = [b['_id'] for b in problems].index(
            boulder_id)  # index of current boulder in list

    keep_searching = True if problems and idx != - \
        1 and idx != len(problems)-1 else False
    gym_code = problems[idx]['gym'] if idx != -1 else ''
    next_idx = 1

    while keep_searching:
        next_boulder = get_boulder_by_id(
            problems[idx+next_idx]['gym'], problems[idx+next_idx]['_id'], database)
        # check if wall section is latest wall set (wrap in function)
        valid_gym_sections = [wall['image'] for wall in get_gym_walls(
            problems[idx+next_idx]['gym'], database, latest_wall_set)]
        # valid boulder, if there are more conditions, add here
        if bool(next_boulder) and next_boulder['section'] in valid_gym_sections:
            keep_searching = False
            gym_code = problems[idx+next_idx]['gym']
        elif next_idx + 1 == len(problems):
            next_boulder = {}
            gym_code = problems[idx]['gym']
            keep_searching = False
        else:
            next_idx += 1

    return next_boulder, gym_code


@serializable
@postprocess_boulder_data
def get_previous_boulder_from_user_list(boulder_id, list_id, user_id, latest_wall_set, sort_by, is_ascending, to_show, database):
    # problems = get_user_problem_list_by_id(user_id, list_id, database)

    SORTING_FIELD_MAP = {
        'creation_date': '_id',  # insertion order is by date
        'difficulty': 'difficulty_int',
        'section': 'section',
        'rating': 'rating',
        # Here we might have problems if not all boulders have repetitions
        'repetitions': 'repetitions'
    }
    REVERSE_MAPS = {
        'green': 0,
        'blue': 1,
        'yellow': 2,
        'red': 3
    }

    # What a pain to have to recover all boulders...
    ticklist_p = get_user_problem_list_by_id(user_id, list_id, database)
    problems = [get_boulder_by_id(b['gym'], b['iden'], database)
                for b in ticklist_p]
    # match fields
    for p in problems:
        for t in ticklist_p:
            if p['_id'] == t['iden']:
                p['gym'] = t['gym']
                p['is_done'] = t['is_done']
                p['difficulty_int'] = REVERSE_MAPS[p['difficulty']]

    # Apply sorting and filtering criteria
    if to_show == 'done':
        problems = [p for p in problems if p['is_done']]
    elif to_show == 'to_do':
        problems = [p for p in problems if not p['is_done']]
    # sorted_problem_list = sorted(problems, key=lambda p: (p[SORTING_FIELD_MAP[sort_by]], (-1 if not is_ascending else 1) *datetime.timestamp(datetime.fromisoformat(p['time']))), reverse=not is_ascending)
    problems.sort(key=lambda p: (p[SORTING_FIELD_MAP[sort_by]], (1 if not is_ascending else -1)
                  * datetime.timestamp(datetime.fromisoformat(p['time']))), reverse=not is_ascending)

    next_boulder = {}

    idx = -1
    if problems:
        # wrap in try catch ? if not found we can keep showing the current boulder
        idx = [b['_id'] for b in problems].index(
            boulder_id)  # index of current boulder in list

    keep_searching = True if problems and idx != -1 and idx != 0 else False
    gym_code = problems[idx]['gym'] if idx != -1 else ''
    next_idx = -1

    while keep_searching:
        next_boulder = get_boulder_by_id(
            problems[idx+next_idx]['gym'], problems[idx+next_idx]['_id'], database)
        # check if wall section is latest wall set (wrap in function)
        valid_gym_sections = [wall['image'] for wall in get_gym_walls(
            problems[idx+next_idx]['gym'], database, latest_wall_set)]
        # valid boulder, if there are more conditions, add here
        if bool(next_boulder) and next_boulder['section'] in valid_gym_sections:
            keep_searching = False
            gym_code = problems[idx+next_idx]['gym']
        elif next_idx - 1 == 0:  # no more problems in list from where to search
            next_boulder = {}
            gym_code = problems[idx]['gym']
            keep_searching = False
        else:
            next_idx -= 1

    return next_boulder, gym_code


@serializable
@postprocess_boulder_data
def get_previous_boulder(
        boulder_id: str,
        gym: str,
        user_id: str,
        latest_wall_set: bool,
        sort_by: str,
        is_ascending: bool,
        to_show: str,
        database: Database) -> Data:
    """
    Given a boulder id, get the previous boulder based on insertion date

    :param boulder_id: boulder ID for which to get next boulder
    :type boulder_id: str
    :param gym: gym code of the boulders through which to iterate
    :type gym: str
    :param database: database connection
    :type database: Database
    :return: next boulder if there is any, empty dict otherwise
    :rtype: Data
    """
    # TODO: query can be reworked so that all happens in the DDBB and not all
    # problems have to be retrieved
    # build the query
    SORTING_FIELD_MAP = {
        'creation_date': '_id',  # insertion order is by date
        'difficulty': 'difficulty',
        'section': 'section',
        'rating': 'rating',
        # Here we might have problems if not all boulders have repetitions
        'repetitions': 'repetitions'
    }
    sorting_field = SORTING_FIELD_MAP[sort_by]

    query_builder = QueryBuilder()

    if latest_wall_set:
        walls = get_gym_walls(gym, database, latest_wall_set)
        query_builder.contained_in(
            'section', [wall['image'] for wall in walls])

    boulders = list(
        database[f'{gym}_boulders'].find(query_builder.query).sort([
            (sorting_field, 1 if is_ascending else -1),
            ('time', -1)
        ])
    )

    # if show only to do, remove problems present as done in user ticklist
    if to_show == 'to_do' and user_id:
        done_boulders = [b['iden'] for b in get_user_problem_list_by_id(
            user_id, 'ticklist', database) if b['is_done'] == True]
        boulders = [boulder for boulder in boulders if str(
            boulder['_id']) not in done_boulders]
        # [(b['name'], b['difficulty'], b['time']) for b in sorted(a, key=lambda x: (-x['difficulty'], -(datetime.datetime.strptime(x['time'], '%Y-%m-%dT%H:%M:%S.%f') - datetime.datetime(1, 1, 1)).total_seconds()))]

    previous_boulder = {}
    if boulders:
        idx = [str(b['_id']) for b in boulders].index(boulder_id)
        if idx > 0:
            previous_boulder = boulders[idx-1]
        else:
            previous_boulder = boulders[idx]
    return previous_boulder


@serializable
def update_boulder_by_id(gym: str, boulder_id: str, boulder_data: Data, database: Database) -> UpdateResult:
    """
    Given a boulder id, a Gym, and new boulder data update the
    whole body of data for that boulder
    """
    boulder_data.pop('_id', None)
    return database[f'{gym}_boulders'].update_one(
        {'_id': ObjectId(boulder_id)},
        {'$set': preprocess_boulder_data(boulder_data)}
    )


@serializable
@postprocess_boulder_data
def get_boulders_filtered(
    gym: str,
    database: Database,
    latest_walls_only: bool,
    conditions: Optional[dict] = None,
    equals: Optional[list] = None,
    ranged: Optional[list] = None,
    contains: Optional[list] = None
) -> dict[str, list[Data]]:
    """
    Given a gym and a set of conditions return the list of boulders
    that fulfill them

    The returned dictionary has one key-value pair.
    The key is 'Items' and the value is a list of boulder data.
    """
    query_builder = QueryBuilder()
    # if get only for latest wall, get latest wall name and add to filters
    # add condition to query -> db.collection.find( { field: { $in: [ 'hi' , 'value'] } } )
    if latest_walls_only:
        walls = get_gym_walls(gym, database, True)
        query_builder.contained_in(
            'section', [wall['image'] for wall in walls])

    # if there are no conditions, return everything
    if not conditions:
        return {ITEMS: list(database[f'{gym}_boulders'].find(query_builder.query))}

    # if there are conditions, apply filters
    for key, value in conditions.items():
        if key in equals:
            query_builder.equal(key, value)
        elif key in contains:
            query_builder.contains_text(key, value)
        elif key in ranged:
            query_builder.lower(key, int(value) + 0.5)
            query_builder.greater(key, int(value) - 0.5)

    filtered_boulder_data = list(
        database[f'{gym}_boulders'].find(query_builder.query))

    if not filtered_boulder_data:
        filtered_boulder_data = list(database[f'{gym}_boulders'].find())
    return {ITEMS: filtered_boulder_data}


@serializable
@postprocess_boulder_data
def get_circuits_filtered(
    gym: str,
    database: Database,
    latest_walls_only: bool
) -> dict[str, list[Data]]:
    """
    Given a gym and a set of conditions return the list of boulders
    that fulfill them

    The returned dictionary has one key-value pair.
    The key is 'Items' and the value is a list of boulder data.
    """
    query_builder = QueryBuilder()
    # if get only for latest wall, get latest wall name and add to filters
    # add condition to query -> db.collection.find( { field: { $in: [ 'hi' , 'value'] } } )
    if latest_walls_only:
        walls = get_gym_walls(gym, database, True)
        query_builder.contained_in(
            'section', [wall['image'] for wall in walls])

    return {ITEMS: list(database[f'{gym}_circuits'].find(query_builder.query))}


# User related functions

@serializable
def save_user(user_data: Data, database: Database) -> InsertOneResult:
    """
    Persist user data. Insert user_data in the given database
    """
    query_builder = QueryBuilder().equal('id',  user_data.get('id', None))
    found_user = database['users'].find_one(query_builder.query)
    if not found_user:
        return database['users'].insert_one(user_data)

    id_query = QueryBuilder().equal('_id', ObjectId(user_data['_id']))
    user_data = {key: val for key, val in user_data.items() if key != '_id'}
    updated_data = {"$set": user_data}
    database['users'].update_one(id_query.query, updated_data)


@serializable
def get_user_data_by_id(user_id: str, database: Database) -> Data:
    """
    Given a user id get its data. Return an empty dictionary if the user is not found
    """
    query_builder = QueryBuilder().equal('id', user_id)
    user = database['users'].find_one(query_builder.query)
    return user if user else {}


@serializable
def get_user_data_by_email(email: str, database: Database) -> Data:
    """
    Given a user email get its data. Return an empty dictionary if the user is not found
    """
    query_builder = QueryBuilder().equal('email', email)
    user = database['users'].find_one(query_builder.query)
    return user if user else {}


@serializable
def get_user_data_by_username(name: str, database: Database) -> Data:
    """
    Given a user email get its data. Return an empty dictionary if the user is not found
    """
    query_builder = QueryBuilder().equal('name', name)
    user = database['users'].find_one(query_builder.query)
    return user if user else {}


@serializable
def get_user_preferences(user_id: str, database: Database) -> Data:
    """
    Given a user id, get its preferences. Return an empty dict if not found
    """
    query_builder = QueryBuilder().equal('user_id', user_id)
    user_prefs = database['user_preferences'].find_one(query_builder.query)
    return user_prefs if user_prefs else {}


@serializable
def save_user_preferences(user_prefs: Data, database: Database) -> InsertOneResult:
    """
    Save a specific user preferences 
    """
    found_user_prefs = database['user_preferences'].find_one(
        QueryBuilder().equal('user_id', user_prefs.get('user_id', None)).query
    )

    if not found_user_prefs:
        return database['user_preferences'].insert_one(user_prefs)

    new_prefs = {key: val for key, val in user_prefs.items() if key != '_id'}
    updated_prefs = {"$set": new_prefs}
    id_query = QueryBuilder().equal('_id', ObjectId(user_prefs['_id']))
    database['user_preferences'].update_one(id_query.query, updated_prefs)
