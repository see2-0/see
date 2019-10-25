# -*- coding: utf-8 -*-
import re
import copy
import time
import json
import subprocess
import configparser
from django.conf import settings
from rest_framework.exceptions import ParseError
from utils.tasks import send_mail
from utils.basemixins import HttpMixin, AppellationMixins, PromptMixins
from utils.dbcrypt import prpcrypt
from utils.basecomponent import DateEncoder
from utils.baseviews import ReturnFormatMixin as res
from utils.sqltools import Inception, SqlQuery, AutoQuery
from utils.exceptions import NotValid
from utils.lock import RedisLock
from utils.wrappers import timer
from .data import inception_conn
from .models import *

class GuardianPermission(object):

    permission_models = []

    def get_related_status(self, instance):
        for model in self.permission_models:
            if model.objects.filter(object_pk=instance.id):
                return 1
        return 0

    def delete_relation(self, instance):
        for model in self.permission_models:
            queryset_permission = model.objects.filter(object_pk=instance.id)
            queryset_permission.delete()

class FixedDataMixins(object):

    def get_queryset(self):
        model = self.serializer_class.Meta.model
        objects = model.objects
        queryset = objects.all()
        if queryset.count() != len(self.source_data):
            queryset.delete()
            data = [model(**row) for row in self.source_data]
            objects.bulk_create(data)
            queryset = objects.all()
        return queryset

class ChangeSpecialCharacterMixins(object):
    special_character_list = ['*']
    transference_character = '\\'

    def convert(self, forbidden_words):
        forbidden_words_list = forbidden_words.split('/')
        forbidden_list = []
        for word in forbidden_words_list:
            if word:
                if word in self.special_character_list:
                    word = '{}{}'.format(self.transference_character, word)
                forbidden_list.append(word)
        return forbidden_list

    def reverse(self, forbidden_list):
        fb_words = []
        for word in forbidden_list:
            if self.transference_character in word:
                word = word.replace(self.transference_character, '')
            fb_words.append(word)
        if len(fb_words) == 1:
            return fb_words[0]
        return fb_words

class InceptionConn(object):
    error_tag = 'error'
    model = InceptionConnection

    def get_cmd(self, sub_cmd):
        conn = self.get_inception_conn()
        return '{} -e "{}" '.format(conn, sub_cmd)

    def get_inception_conn(self):
        instance = self.model.objects.first()
        obj = instance or self.model.objects.get_or_create(**inception_conn[0])[0]
        return 'mysql -h{} -P{}'.format(obj.host, obj.port)

class CheckConn(InceptionConn, AutoQuery):
    conf = configparser.ConfigParser()
    file_path = settings.INCEPTION_SETTINGS.get('file_path')

    def handle_get_databases(self, request):
        ret = res.get_ret()
        databases = self.get_databases(request.data)
        ret['data'] = [item[0] for item in databases]
        return ret

    def inception_conn(self, *args):
        ret = res.get_ret()
        sub_cmd = "inception get variables"
        cmd = self.get_cmd(sub_cmd)
        result = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        lines = result.stdout.readlines()
        last_item = lines[-1].decode('gbk') if len(lines) > 0 else ''
        if self.error_tag in last_item.lower():
            ret['status'] = -1
            ret['data'] = last_item
        return ret

    def inception_backup(self, request):
        self.conf.read(self.file_path)
        password = self.conf.get('inception', 'inception_remote_system_password')
        params = request.data
        params['password'] = password
        params['db'] = 'inception'
        self.conn_database(params)
        return res.get_ret()

    def update_target_db(self, request):
        pk = request.data.get('id')
        instance = Dbconf.objects.get(pk=pk)
        params = {
            'db': instance.name,
            'host': instance.host,
            'port': instance.port,
            'user': instance.user,
            'password': prpcrypt.decrypt(instance.password)
        }
        self.conn_database(params)
        return res.get_ret()

class HandleInceptionSettingsMixins(InceptionConn):
    backup_variables = [
        'inception_remote_backup_host',
        'inception_remote_backup_port',
        'inception_remote_system_user',
        'inception_remote_system_password'
    ]

    def get_inception_backup(self):
        return {variable:self.get_status(variable) for variable in self.backup_variables}

    def get_status(self, variable_name):
        filter_words = [variable_name, '\t', '\n']
        sub_cmd = "inception get variables '{}'".format(variable_name)
        cmd = self.get_cmd(sub_cmd)
        result = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        lines = result.stdout.readlines()
        if not lines:
            return None
        ret = lines[-1].decode('gbk')
        if self.error_tag in ret.lower():
            return None
        for word in filter_words:
            ret = ret.replace(word, '')
        return ret

    def set_variable(self, request):
        request_data = request.data
        variable_name = request_data.get('variable_name')
        variable_value = request_data.get('variable_value')
        sub_cmd = "inception set {}={}".format(variable_name, variable_value)
        cmd = self.get_cmd(sub_cmd)
        subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

class CheckStatusMixins(HttpMixin, AppellationMixins, PromptMixins):

    @property
    def init(self):
        status_map = \
        {
            self.urn_execute: {
                'status_list': [-1, 3],
                'warning': self.action_status_warning_execute
            },
            self.urn_reject: {
                'status_list': [-2, -1, 3, 4, 6],
                'warning': self.action_status_warning_reject
            },
            self.urn_approve: {
                'status_list': [-1],
                'warning': self.action_status_warning_approve
            },
            self.urn_disapprove: {
                'status_list': [-1],
                'warning': self.action_status_warning_approve
            },
            self.urn_rollback: {
                'status_list': [0],
                'warning': self.action_status_warning_rollback
            },
            self.urn_cron: {
                'status_list': [-1, 3, 5],
                'warning': self.action_status_warning_cron
            },
            self.urn_database_order_approve: {
                'status_list': [0],
                'warning': self.action_status_warning_database_order_manage
            },
            self.urn_database_order_disapprove: {
                'status_list': [0],
                'warning': self.action_status_warning_database_order_manage
            },
            self.urn_database_order_reject: {
                'status_list': [0, 1, 2],
                'warning': self.action_status_warning_database_order_reject
            }

        }
        return status_map

    def check_status(self, instance):
        action = self.get_urls_action(self.request)
        status_list = self.init[action]['status_list']
        warning = self.init[action]['warning']
        if instance.status not in status_list:
            raise ParseError(warning)

class ActionMixins(HttpMixin, AppellationMixins, PromptMixins):

    type_select_tag = 'select'
    action_type_execute = '--enable-execute'
    action_type_check = '--enable-check'
    success_tag = 'Execute Successfully\nBackup successfully'

    def get_reject_step(self, instance):
        user = self.request.user
        if self.has_flow(instance):
            if user.is_superuser:
                return 1 if instance.commiter == user.username else 2
            else:
                return self.reject_steps.get(user.role)

    @staticmethod
    def get_current_step(instance):
        steps = instance.workorder.step_set.all()
        current = 0
        for step in steps:
            if step.status not in [-1, 0]:
                current += 1
        return current

    @property
    def is_manual_review(self):
        instance = Strategy.objects.first()
        if not instance:
            instance = Strategy.objects.create()
        return instance.is_manual_review

    def handle_workflow(self, call_type, status, step_number, instance=None):
        instance = instance or self.get_object()
        if self.has_flow(instance):
            if call_type == 1:
                if status == 1:
                    self.save_instance(instance.workorder, True)
            step_instance = instance.workorder.step_set.order_by('id')[step_number]
            self.save_instance(step_instance, status)
            if call_type == 3:
                steps = instance.workorder.step_set.all()
                steps_behind = steps.filter(id__gt=step_instance.id)
                for step in steps_behind:
                    self.save_instance(step, -1)

    def get_db_conf(self, user, password, host, port, actiontype):
        password = prpcrypt.decrypt(password)
        return '--user={}; --password={}; --host={}; --port={}; {};'.format(user, password, host, port, actiontype)

    def has_flow(self, instance):
        return instance.is_manual_review is True and instance.env == self.env_prd

    @classmethod
    def save_instance(cls, instance, status=None):
        if status is not None:
            instance.status = status
        instance.save()

    def check_lock(self, instance):
        if not RedisLock.locked(instance.id):
            raise ParseError(self.task_locked.format(instance.id))

    def filter_select_type(self, instance):
        if instance.type == self.type_select_tag:
            raise ParseError(self.type_warning)

    def check_valid_date(self, cron_time):
        date_format = settings.CELERY_BUSINESS_PARAMS.get('date_format')
        try:
            time.mktime(time.strptime(cron_time, date_format))
        except Exception:
            raise ParseError(self.invalid_date_warning.format(cron_time))

    def filter_date(self, queryset):
        date_range = self.request.GET.get('daterange')
        if queryset and date_range:
            return queryset.filter(createtime__range=date_range.split(','))
        return queryset

    def check_rollback_able(self, instance):
        if not instance.rollback_able:
            raise ParseError(self.not_rollback_able)

    def check_execute_sql(self, db_id, sql_content, action_type,triggerstmp=''):
        db_instance = Dbconf.objects.get(id=db_id)
        db_conf = self.get_db_conf(db_instance.user, db_instance.password, db_instance.host, db_instance.port, action_type)
        sql_review = Inception(sql_content, db_instance.name).inception_handle(db_conf,triggerstmp)
        result, status = sql_review.get('result'), sql_review.get('status')
        if status == -1 or len(result) == 1:
            raise ParseError({self.connect_error: result})
        success_sql_list = []
        exception_sql_list = []
        for sql_result in result:
            error_message = sql_result[4]
            if error_message == 'None':
                success_sql_list.append(sql_result)
            else:
                exception_sql_list.append(error_message)
        if exception_sql_list and action_type == self.action_type_check:
            raise NotValid(exception_sql_list)
        return success_sql_list, exception_sql_list, json.dumps(result)

    def replace_remark(self, instance, action=None, user=None):
        user = user or self.request.user
        username = user.username
        action = action or self.get_urls_action(self.request)
        if username != instance.treater:
            instance.remark +=  '   [' + username + self.action_desc_map.get(action) + ']'
        if instance.workorder.status is True:
            steps = instance.workorder.step_set.all()
            step_obj_second = steps[1]
            if user and not (user == step_obj_second.user and action == 'reject'):
                step_obj = steps[0]
                step_obj.user = user
                self.save_instance(step_obj)

class MailMixin(AppellationMixins):

    def mail(self, sqlobj, mail_type, personnel, source_app=''):
        try:
            mail_action = MailActions.objects.get(name=mail_type)
            if (sqlobj.env == self.env_prd):
                treater = sqlobj.treater
                commiter = sqlobj.commiter
                user = User.objects.get(username=commiter)
                admin_mail = user.admin_mail.username if user.admin_mail else None
                mailto_users = [treater, commiter, admin_mail]
                mailto_users = list(set(mailto_users))
                mailto_list = [u.email for u in User.objects.filter(username__in = mailto_users)]
                atuser = [u.phone for u in User.objects.filter(username = treater)]
                send_mail(mailto_list, personnel.email, sqlobj.id, sqlobj.remark, mail_type, sqlobj.sql_content, sqlobj.db.name,atuser)
        except Exception as e:
            print(e)

class Handle(ActionMixins):

    @timer
    def select(self, instance):
        sql_query = SqlQuery(instance.db)
        sqlcontent = instance.sql_content
        sqlcontent = re.sub(r"/\*[^*]*\*+(?:[^*/][^*]*\*+)*/", "", sqlcontent)
        lines = [line for line in sqlcontent.splitlines() if not re.match("^\s*(--|#)", line)]
        sqlcontent = " ".join([re.split("--|# ", line)[0] for line in lines])
        status, data = sql_query.get_select_result(sqlcontent)
        instance.handle_result_execute = json.dumps([str(row) for row in data], cls=DateEncoder)
        instance.status = status
        return instance, len(data)

    @timer
    def execute(self, instance):
        affected_rows = 0
        opid_list = []
        instance.status = 0
        success_sql_num = 0
        sqlcontent = instance.sql_content
        sqlcontent = re.sub(r"/\*[^*]*\*+(?:[^*/][^*]*\*+)*/", "", sqlcontent)
        lines = [line for line in sqlcontent.splitlines() if not re.match("^\s*(--|#)", line)]
        sqlcontent = " ".join([re.split("--|# ", line)[0] for line in lines])
        triggers_obj = SqlQuery(instance.db)
        tablename = triggers_obj.extract_table_name_from_sql(sqlcontent)
        triggerstmp = ''
        if tablename:
            triggerstmp = triggers_obj.get_triggers(tablename)
        success_sql_list, exception_sql_list, handle_result_execute = self.check_execute_sql(instance.db.id, sqlcontent, self.action_type_execute,triggerstmp)
        for success_sql in success_sql_list:
            affected_rows += success_sql[6]
            if re.findall(self.success_tag, success_sql[3]):
                success_sql_num += 1
                opid_list.append(success_sql[7].replace("'", ""))
        rollback_able = True if success_sql_num == len(success_sql_list) - 1 else False
        if exception_sql_list:
            instance.status = 2
            instance.execute_errors = exception_sql_list
        instance.rollback_db = success_sql[8]
        instance.rollback_opid = opid_list
        instance.rollback_able = rollback_able
        instance.handle_result_execute = handle_result_execute
        return instance, affected_rows

    @timer
    def rollback(self, instance):
        self.filter_select_type(instance)
        self.check_rollback_able(instance)
        db_instance = instance.db
        rollback_opid_list = instance.rollback_opid
        rollback_db = instance.rollback_db  # 回滚库
        # 拼接回滚语句
        back_sql_list = ''  # 回滚语句
        for opid in eval(rollback_opid_list):
            # 1 从回滚总表中获取表名
            back_source = 'select tablename from $_$Inception_backup_information$_$ where opid_time = "{}" '.format(opid)
            back_table = Inception(back_source, rollback_db).get_back_table()
            # 2 从回滚子表中获取回滚语句
            statement_sql = 'select rollback_statement from {} where opid_time = "{}" '.format(back_table, opid)
            rollback_statement = Inception(statement_sql, rollback_db).get_back_sql()
            if not rollback_statement:
                instance.status = 2
                instance.handle_result_rollback = json.dumps([self.get_rollback_fail])
                return instance, instance.affected_rows
            back_sql_list += rollback_statement
        db_conf = self.get_db_conf(db_instance.user, db_instance.password, db_instance.host, db_instance.port, self.action_type_execute)
        execute_results = Inception(back_sql_list, db_instance.name).inception_handle(db_conf).get('result')
        status = -3
        for result in execute_results:
            if result[4] != 'None':
                status = 2
                break
        instance.status = status
        instance.handle_result_rollback = json.dumps(execute_results)
        return instance, instance.affected_rows
