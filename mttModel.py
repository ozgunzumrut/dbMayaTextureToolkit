# Maya import
import maya.cmds as cmds

# Qt import
from PySide.QtCore import *
from PySide.QtGui import QItemSelectionModel

# Python import
import sys
import sqlite3
import re

# custom import
from mttConfig import *


class MTTModel(QAbstractTableModel):
    """
    Data structure of textures nodes
    """

    def __init__(self, settings=None, watcher=None):
        """ Init model """
        QAbstractTableModel.__init__(self)
        self.table_view = None
        self.settings = settings
        self.filewatcher = watcher
        self.filewatcher.fileChanged.connect(self.filewatch_file_change)
        self.filewatcher.directoryChanged.connect(self.filewatch_directory_change)
        self.is_reloading_file = False
        self.suspend_force_sort = False
        self.supported_format_dict = dict([(nodeType, nodeAttr) for nodeType, nice, nodeAttr in SUPPORTED_TYPE])
        self.db = None
        # create database table
        try:
            self._database_create_table()
        except sqlite3.Error, e:
            db_output('Error init database :\n\t>> %s <<\n' % e, msg_type='error')
            sys.exit(1)

        # populate database
        self.textures = self._database_populate()

    def _database_create_table(self):
        """ Create database table """
        self.database_close()
        self.db = sqlite3.connect(':memory:')
        c = self.db.cursor()

        c.execute(
            'CREATE TABLE NodesTable('
            'Id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'Name TEXT, '
            'Type TEXT, '
            'Attribute TEXT, '
            'IsRef INTEGER, '
            'FileId INTEGER, '
            'RefName TEXT)'
        )
        c.execute(
            'CREATE TABLE FilesTable('
            'FileId INTEGER PRIMARY KEY AUTOINCREMENT, '
            'KeyPath TEXT, '
            'FilePath TEXT, '
            'State INTEGER, '
            'InstanceCount INTEGER)'
        )
        c.execute(
            'CREATE TABLE RefTable('
            'RefName TEXT PRIMARY KEY, '
            'RefPath TEXT, '
            'RefSourceImage TEXT)'
        )

    #noinspection PyUnusedLocal
    def _database_populate(self):
        """ Populate database """
        # get cursor
        c = self.db.cursor()

        # add current workspace to first key of RefTable
        current_sourceimage_folder = 'sourceimages'
        if 'sourceImages' in cmds.workspace(fileRuleList=True):
            current_sourceimage_folder = cmds.workspace(fileRuleEntry="sourceImages")
        workspace_path = cmds.workspace(query=True, rootDirectory=True)
        c.execute(
            'INSERT INTO RefTable(RefName, RefPath, RefSourceImage) VALUES (?, ?, ?)',
            ('ROOT', workspace_path, current_sourceimage_folder)
        )

        # parse all nodes with supported type
        for node_type, type_nicename, node_attr in SUPPORTED_TYPE:

            nodes = cmds.ls(exactType=node_type)

            for node in nodes:

                ref_name = 'ROOT'

                # special reference node case
                is_reference_node = cmds.referenceQuery(node, isNodeReferenced=True)
                if is_reference_node:
                    is_new, ref_name, root_path, sourceimages_folder = self.get_reference_info(node)

                    if is_new:
                        c.execute(
                            'INSERT INTO RefTable(RefName, RefPath, RefSourceImage) VALUES (?, ?, ?)',
                            (ref_name, root_path, sourceimages_folder)
                        )

                # format nicename
                if type_nicename is '' or type_nicename is None:
                    type_nicename = node_type

                node_attr_value = cmds.getAttr('%s.%s' % (node, node_attr))
                file_path = self.get_attribute_absolute_file_path(node, node_attr_value)
                last_id = self.database_add_file(file_path)

                # add current node to database
                c.execute(
                    'INSERT INTO NodesTable(Name, Type, Attribute, IsRef, FileId, RefName) VALUES (?, ?, ?, ?, ?, ?)',
                    (node, type_nicename, node_attr_value, is_reference_node, last_id, ref_name)
                )

        self.db.commit()

        # return node texture list
        c.execute('SELECT Name, Type, IsRef FROM NodesTable')

        return c.fetchall()

    def database_reset(self):
        self._database_create_table()
        self.textures = self._database_populate()
        self.reset()
        self.request_sort()

    def database_close(self):
        """ Close database connection """
        if self.db:
            self.db.close()
            self.db = None

    def database_add_new_node(self, node_name):
        node_type = cmds.nodeType(node_name)
        type_nicename, node_attr_name = self.get_nicename_and_attribute_name(node_type)
        node_attr_value = cmds.getAttr('%s.%s' % (node_name, node_attr_name))
        file_path = self.get_attribute_absolute_file_path(node_name, node_attr_value)
        last_id = self.database_add_file(file_path)

        # add current node to database
        c = self.db.cursor()
        c.execute(
            'INSERT INTO NodesTable(Name, Type, Attribute, IsRef, FileId, RefName) VALUES (?, ?, ?, ?, ?, ?)',
            (node_name, type_nicename, node_attr_value, False, last_id, 'ROOT')
        )

    def database_add_file(self, file_path):
        file_state = self.get_file_state(file_path)
        instance_count = self.get_file_instance_count(file_path)
        key_path = self.convert_to_key_path(file_path)

        c = self.db.cursor()
        if instance_count == 0:
            # register current file data
            c.execute(
                'INSERT INTO FilesTable(KeyPath, FilePath, State, InstanceCount) VALUES (?, ?, ?, ?)',
                (key_path, file_path, file_state, 1)
            )
            last_id = c.lastrowid
            self.filewatch_add_path(file_path)
        else:
            # update current file data
            c.execute('UPDATE FilesTable SET InstanceCount=InstanceCount + 1 WHERE KeyPath=?', (key_path, ))
            c.execute('SELECT FileId FROM FilesTable WHERE KeyPath=?', (key_path, ))
            last_id = c.fetchone()[0]

        return last_id

    def database_remove_node(self, node_name):
        model_id = self.get_node_model_id(node_name)
        self.beginRemoveRows(QModelIndex(), model_id.row(), model_id.row())
        c = self.db.cursor()
        c.execute('SELECT FileId FROM NodesTable LEFT JOIN FilesTable USING (FileId) WHERE Name=?', (node_name, ))
        file_id = c.fetchone()[0]
        c.execute('SELECT InstanceCount FROM FilesTable WHERE FileId=?', (file_id, ))
        if c.fetchone()[0] == 1:
            c.execute('DELETE FROM FilesTable WHERE FileId=?', (file_id, ))
        else:
            c.execute('UPDATE FilesTable SET InstanceCount=InstanceCount - 1 WHERE FileId=?', (file_id, ))
        c.execute('DELETE FROM NodesTable WHERE Name=?', (node_name, ))
        self.endRemoveRows()

    def get_database_content_as_csv(self):
        c = self.db.cursor()
        c.execute(
            'SELECT Name, Type, IsRef, State, InstanceCount, FilePath FROM NodesTable as N '
            'LEFT JOIN FilesTable as F ON N.FileId=F.FileId '
            'LEFT JOIN RefTable as R ON N.RefName=R.RefName')

        return c.fetchall()

    def database_dump_csv(self):
        c = self.db.cursor()
        c.execute(
            'SELECT * FROM NodesTable as N '
            'LEFT JOIN FilesTable as F ON N.FileId=F.FileId '
            'LEFT JOIN RefTable as R ON N.RefName=R.RefName')

        import csv
        file_path = os.path.join(os.path.dirname(__file__), 'debug_db.csv')
        csv_file = open(file_path, "w")
        csv_file_writer = csv.writer(csv_file, delimiter=';')
        csv_file_writer.writerows(c.fetchall())
        csv_file.close()
        db_output('CSV Dump write into : %s' % file_path, add_tag='DEBUG')
        cmds.launchImageEditor(viewImageFile=os.path.dirname(__file__))

    def database_dump_sql(self):
        sql_file = os.path.join(os.path.dirname(__file__), 'debug_db.sql')
        with open(sql_file, 'w') as f:
            for line in self.db.iterdump():
                f.write('%s\n' % line)
        db_output('SQL Dump write into : %s' % sql_file, add_tag='DEBUG')
        cmds.launchImageEditor(viewImageFile=os.path.dirname(__file__))

    def flags(self, index):
        """ Define editable cells """
        if not index.isValid():
            return Qt.ItemIsEnabled

        if index.column() in [NODE_NAME, NODE_FILE]:
            return Qt.ItemFlags(QAbstractTableModel.flags(self, index) | Qt.ItemIsEditable)

        return Qt.ItemFlags(QAbstractTableModel.flags(self, index))

    def data(self, index, role=Qt.DisplayRole):
        """ Define value for current cells """
        if not index.isValid() or not (0 <= index.row() < self.rowCount()):
            return None

        texture = self.textures[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            if column == NODE_NAME:
                return texture[0]
            elif column == NODE_TYPE:
                return texture[1]
            elif column == NODE_REFERENCE:
                return texture[2]
            elif column == FILE_STATE:
                return self.get_node_file_state(texture[0])
            elif column == FILE_COUNT:
                return self.get_node_instance_count(texture[0])
            elif column == NODE_FILE:
                norm_path = os.path.normpath(self.get_node_attribute(texture[0]))
                if norm_path == '.':
                    norm_path = ''
                return norm_path

        elif role == Qt.TextAlignmentRole:
            if column == FILE_COUNT:
                return int(Qt.AlignCenter | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)

        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        """ Header label """
        if role != Qt.DisplayRole:
            return None

        if orientation == Qt.Horizontal:
            if section == NODE_NAME:
                return VIEW_COLUMN_LABEL[NODE_NAME]
            elif section == NODE_REFERENCE:
                return VIEW_COLUMN_LABEL[NODE_REFERENCE]
            elif section == NODE_TYPE:
                return VIEW_COLUMN_LABEL[NODE_TYPE]
            elif section == FILE_STATE:
                return VIEW_COLUMN_LABEL[FILE_STATE]
            elif section == FILE_COUNT:
                return VIEW_COLUMN_LABEL[FILE_COUNT]
            elif section == NODE_FILE:
                return VIEW_COLUMN_LABEL[NODE_FILE]

        return int(section + 1)

    def setData(self, index, value, role=Qt.EditRole):
        """ Update data from model """
        if index.isValid() and 0 <= index.row() < self.rowCount():
            texture = self.textures[index.row()]
            node_name = texture[0]
            column = index.column()

            if column == NODE_NAME:
                wanted_name = value
                if wanted_name:
                    new_name = self.rename_maya_node(node_name, wanted_name)
                    state = new_name != node_name
                    if state:
                        self.request_sort()
                    return state
                else:
                    return False
            elif column == NODE_FILE:
                cmds.setAttr(
                    '%s.%s' % (node_name, self.supported_format_dict[cmds.nodeType(node_name)]),
                    value,
                    type='string'
                )
                self.request_sort()
                return True

        return False

    # noinspection PyUnusedLocal
    def rowCount(self, parent=QModelIndex(), *args, **kwargs):
        return len(self.textures)

    # noinspection PyUnusedLocal
    def columnCount(self, parent=QModelIndex(), *args, **kwargs):
        return COLUMN_COUNT

    def sort(self, column_id, sort_order=None):
        # get current selection
        current_selection = list()
        current_node_name = ''
        current_proxy = None
        if self.table_view:
            current_selection = [idName.data() for idName in self.table_view.selectionModel().selectedRows()]
            current_model_id = self.table_view.selectionModel().currentIndex()
            current_proxy = current_model_id.model()
            current_node_name = (
                current_model_id.data()
                if current_model_id.column() == 0
                else current_model_id.sibling(current_model_id.row(), NODE_NAME).data()
            )
            #self.table_view.selectionModel().clear()
            #self.table_view.selectionModel().clearSelection()
            self.table_view.selectionModel().reset()

        # sort data
        self.emit(SIGNAL("layoutAboutToBeChanged()"))

        c = self.db.cursor()
        c.execute(
            'SELECT Name, Type, IsRef FROM NodesTable as N '
            'LEFT JOIN FilesTable as F ON N.FileId=F.FileId '
            'LEFT JOIN RefTable as R ON N.RefName=R.RefName '
            'ORDER BY %s %s' % (DB_COLUMN_LABEL[column_id], ['ASC', 'DESC'][sort_order]))
        self.textures = c.fetchall()

        cmds.optionVar(stringValue=('filtered_instances', ''))

        self.emit(SIGNAL("layoutChanged()"))

        # set stored selection
        if self.table_view and current_selection and current_proxy and not get_settings_bool_value(self.settings.value('autoSelect', DEFAULT_AUTO_SELECT)):
            new_current_model_id = current_proxy.match(self.index(0, 0, QModelIndex()), Qt.DisplayRole, current_node_name, 1, Qt.MatchExactly)
            if new_current_model_id:
                self.table_view.selectionModel().setCurrentIndex(
                    new_current_model_id[0],
                    QItemSelectionModel.Current | QItemSelectionModel.Rows
                )

            for nodeName in current_selection:
                model_id = current_proxy.match(self.index(0, 0, QModelIndex()), Qt.DisplayRole, nodeName, 1, Qt.MatchExactly)
                if model_id:
                    self.table_view.selectionModel().select(
                        model_id[0],
                        QItemSelectionModel.Select | QItemSelectionModel.Rows
                    )

        # refresh column header visibility
        for columnId in range(COLUMN_COUNT):
            self.table_view.setColumnHidden(
                columnId,
                not get_settings_bool_value(self.settings.value('columnVisibility_%s' % columnId, True)))

    def request_sort(self):
        if not self.suspend_force_sort:
            self.sort(
                self.table_view.horizontalHeader().sortIndicatorSection(),
                self.table_view.horizontalHeader().sortIndicatorOrder()
            )

    @staticmethod
    def validate_node_name(node_name):
        if node_name[0].isdigit():
            node_name = '_%s' % node_name

        return node_name

    def rename_maya_node(self, node_name, wanted_name, deferred=False):
        """ Rename node and return new name """
        if cmds.lockNode(node_name, query=True, lock=True)[0] or cmds.referenceQuery(node_name, isNodeReferenced=True):
            db_output('%s is locked, cannot perform rename\n' % node_name)
            return node_name

        wanted_name = self.validate_node_name(wanted_name)

        if deferred:
            cmd = 'import maya.cmds as cmds;cmds.rename("%s", "%s")' % (node_name, wanted_name)
            return cmds.evalDeferred(cmd)
        else:
            return cmds.rename(node_name, wanted_name)

    def rename_database_node(self, node_name, wanted_name):
        """ update database node name """
        index = self.get_node_model_id(node_name)
        texture = self.textures[index.row()]

        self.textures[index.row()] = (wanted_name, texture[1], texture[2])
        self.db.cursor().execute('UPDATE NodesTable SET Name=? WHERE Name=?', (wanted_name, node_name))
        self.dataChanged.emit(index, index)

    def change_node_attribute(self, node_name, new_attribute_value):
        if cmds.lockNode(node_name, query=True, lock=True)[0] or cmds.referenceQuery(node_name, isNodeReferenced=True):
            db_output('%s is locked, cannot perform changePath\n' % node_name)
            return False

        if self.is_reloading_file:
            return False

        # get original data for current node
        c = self.db.cursor()
        c.execute(
            'SELECT Attribute, FilePath, InstanceCount, FileId FROM NodesTable LEFT JOIN FilesTable USING (FileId) WHERE Name=?',
            (node_name, ))
        node_attr, node_file_path, instance_count, old_file_id = c.fetchone()

        new_absolute_attr_value = self.get_attribute_absolute_file_path(node_name, new_attribute_value)
        key_path = self.convert_to_key_path(new_absolute_attr_value)
        # check if new_attribute_value already exist
        c.execute('SELECT EXISTS (SELECT 1 FROM FilesTable WHERE KeyPath=?)', (key_path, ))
        if c.fetchone()[0] > 0:
            # instance found
            c.execute('SELECT FileId FROM FilesTable WHERE KeyPath=?', (key_path, ))
            new_file_id = c.fetchone()[0]
        else:
            # new entry
            c.execute(
                'INSERT INTO FilesTable(KeyPath, FilePath, State, InstanceCount) VALUES (?, ?, ?, ?)',
                (key_path, new_absolute_attr_value, self.get_file_state(new_absolute_attr_value), 0)
            )
            new_file_id = c.lastrowid
            self.filewatch_add_path(new_absolute_attr_value)

        # update old file instance count and state
        c.execute('SELECT InstanceCount FROM FilesTable WHERE FileId=?', (old_file_id, ))
        if c.fetchone()[0] == 1 and node_file_path != new_absolute_attr_value:
                c.execute('DELETE FROM FilesTable WHERE FileId=?', (old_file_id, ))
        else:
            c.execute('UPDATE FilesTable SET InstanceCount=InstanceCount - 1 WHERE FileId=?', (old_file_id, ))
            c.execute('UPDATE FilesTable SET State=? WHERE FileId=?', (self.get_file_state(node_file_path), old_file_id))

        # set new values
        c.execute('UPDATE NodesTable SET Attribute=? WHERE Name=?', (new_attribute_value, node_name))
        c.execute('UPDATE NodesTable SET FileId=? WHERE Name=?', (new_file_id, node_name))
        c.execute('UPDATE FilesTable SET InstanceCount=InstanceCount + 1 WHERE FileId=?', (new_file_id, ))
        c.execute('UPDATE FilesTable SET State=? WHERE FileId=?', (self.get_file_state(new_absolute_attr_value), new_file_id))

        self.db.commit()

        # notify data changed
        index = self.get_node_model_id(node_name)
        self.dataChanged.emit(index, index)

        return True

    @staticmethod
    def convert_to_key_path(file_path):
        key_path = os.path.normpath(file_path).lower()

        return key_path

    def get_node_count(self):
        """ Return node count """
        c = self.db.cursor()
        c.execute('SELECT COUNT(Name) FROM NodesTable')

        return c.fetchone()[0]

    def get_file_count(self):
        """ Return file count """
        c = self.db.cursor()
        c.execute('SELECT COUNT(FileId) FROM FilesTable WHERE FilePath!="."')

        return c.fetchone()[0]

    def get_all_nodes_name(self):
        """ Return all textures node name """
        c = self.db.cursor()
        c.execute('SELECT Name FROM NodesTable')

        return c.fetchall()

    def get_node_model_id(self, node_name):
        return self.match(self.index(0, 0, QModelIndex()), Qt.DisplayRole, node_name, 1, Qt.MatchExactly)[0]

    def get_node_file_fullpath(self, node_name):
        """ Return full filename """
        c = self.db.cursor()
        c.execute('SELECT FilePath FROM NodesTable LEFT JOIN FilesTable USING (FileId) WHERE Name=?', (node_name, ))

        return c.fetchone()[0]

    def get_node_file_basename(self, node_name):
        """ Return filename without extension """
        c = self.db.cursor()
        c.execute('SELECT FilePath FROM NodesTable LEFT JOIN FilesTable USING (FileId) WHERE Name=?', (node_name, ))

        file_basename = c.fetchone()[0]

        if len(file_basename):
            file_basename = os.path.splitext(os.path.basename(file_basename))[0]

        return file_basename

    @staticmethod
    def get_file_state(file_path):
        if os.path.isdir(file_path):
            file_state = -1
        else:
            if os.access(file_path, os.W_OK):
                file_state = 1
            elif os.access(file_path, os.R_OK):
                file_state = 0
            else:
                file_state = -1

        return file_state

    def get_node_file_state(self, node_name):
        c = self.db.cursor()
        c.execute('SELECT State FROM NodesTable LEFT JOIN FilesTable USING (FileId) WHERE Name=?', (node_name, ))
        return c.fetchone()[0]

    def get_node_instance_count(self, node_name):
        c = self.db.cursor()
        c.execute('SELECT InstanceCount FROM NodesTable LEFT JOIN FilesTable USING (FileId) WHERE Name=?', (node_name, ))
        return c.fetchone()[0]

    def get_node_instances_model_id(self, node_name):
        c = self.db.cursor()
        c.execute('SELECT FileId FROM NodesTable WHERE Name=?', (node_name, ))
        file_id = c.fetchone()[0]
        c.execute('SELECT Name FROM NodesTable WHERE FileId=?', (file_id, ))
        return [self.get_node_model_id(name[0]) for name in c.fetchall()]

    def get_file_instance_count(self, file_path):
        c = self.db.cursor()
        key_path = self.convert_to_key_path(file_path)
        c.execute('SELECT COUNT(*) FROM FilesTable WHERE KeyPath=?', (key_path, ))
        return c.fetchone()[0]

    def get_node_attribute(self, node_name):
        if self.db:
            c = self.db.cursor()
            c.execute('SELECT Attribute FROM NodesTable WHERE Name=?', (node_name, ))
            return c.fetchone()[0] or ''
        else:
            return ''

    def get_reference_info(self, node_name):
        """ Return new entry boolean, reference node, project path and sourceimage path """
        is_new = False
        ref_name = cmds.referenceQuery(node_name, referenceNode=True)
        sourceimages_folder = 'sourceimages'

        # try to get already existing data
        c = self.db.cursor()
        c.execute('SELECT RefPath, RefSourceImage FROM RefTable WHERE RefName=?', (ref_name, ))
        data = c.fetchone()

        # check if entry already exists
        if data is not None:
            root_path = data[0]
            sourceimages_folder = data[1]
        else:
            is_new = True
            # find workspace.mel in parent folder
            root_path = cmds.referenceQuery(ref_name, filename=True)
            for i in range(len(root_path.split('/'))):
                root_path = os.path.dirname(root_path)
                if os.path.isfile(os.path.join(root_path, 'workspace.mel')):
                    #read sourceImages key
                    f = open(os.path.join(root_path, 'workspace.mel'))
                    content = f.read()
                    f.close()
                    m = re.search(r'"sourceImages" "([a-zA-z0-9 \\/]+)', content)
                    if m is not None:
                        sourceimages_folder = m.group(1)
                    break

        return is_new, ref_name, root_path, sourceimages_folder

    def get_attribute_absolute_file_path(self, node_name, node_attr_value):
        """ Return absolute file path """
        if node_attr_value is None:
            return ''
        filename = os.path.basename(node_attr_value)

        if os.path.isfile(node_attr_value) or os.path.isdir(os.path.dirname(node_attr_value)):
            file_path = node_attr_value
        else:
            if cmds.referenceQuery(node_name, isNodeReferenced=True):
                is_new, ref_name, root_path, sourceimages_folder = self.get_reference_info(node_name)
            else:
                c = self.db.cursor()
                c.execute('SELECT RefPath, RefSourceImage FROM RefTable WHERE RefName="ROOT"')
                root_path, sourceimages_folder = c.fetchone()

            # remove first special character
            node_attr_value = node_attr_value.lstrip(r'\/')
            # try to append attr to workspace directory
            if os.path.isfile(os.path.join(root_path, node_attr_value)) or os.path.isdir(os.path.dirname(os.path.join(root_path, node_attr_value))):
                file_path = os.path.join(root_path, node_attr_value)
            #try to resolve environnement variable
            elif os.path.isfile(os.path.expandvars(node_attr_value)):
                file_path = os.path.expandvars(node_attr_value)
            # try to append workspace + sourceimages + texture.ext
            elif os.path.isfile(os.path.join(root_path, sourceimages_folder, filename)):
                file_path = os.path.join(root_path, sourceimages_folder, filename)
            # last solution ?
            else:
                file_path = node_attr_value

        return os.path.normpath(file_path)

    @staticmethod
    def get_nicename_and_attribute_name(node_type):
        for nType, tNiceName, nAttr in SUPPORTED_TYPE:
            if nType == node_type:
                type_nicename = tNiceName
                if tNiceName is '' or tNiceName is None:
                    type_nicename = node_type
                return type_nicename, nAttr
        return 'XXX', 'fileTextureName'

    def get_sourceimages_path(self):
        """ Return source image folder full path """
        c = self.db.cursor()
        c.execute('SELECT RefPath, RefSourceImage FROM RefTable WHERE RefName="ROOT"')
        path, sourceimage = c.fetchone()
        sourceimage_folder = os.path.join(path, sourceimage)
        if os.path.isdir(sourceimage_folder):
            return sourceimage_folder
        else:
            return cmds.workspace(query=True, rootDirectory=True)

    def set_database_node_and_attribute(self, node_name, node_attr_value):
        """ Set absolute or relative file path """
        cmds.setAttr('%s.%s' % (node_name, self.supported_format_dict[cmds.nodeType(node_name)]), node_attr_value, type='string')
        c = self.db.cursor()
        c.execute('UPDATE NodesTable SET Attribute=? WHERE Name=?', (node_attr_value, node_name))

    def filewatch_add_path(self, file_path):
        if os.path.isdir(file_path) or os.path.isfile(file_path):
            self.filewatcher.addPath(file_path)
        elif os.path.isdir(os.path.dirname(file_path)):
            self.filewatcher.addPath(os.path.dirname(file_path))

    def filewatch_remove_all(self):
        self.filewatcher.removePaths(self.filewatcher.files())
        self.filewatcher.removePaths(self.filewatcher.directories())

    def filewatch_directory_change(self, dir_path):
        c = self.db.cursor()
        c.execute('SELECT FilePath FROM FilesTable WHERE State<1')
        db_files = c.fetchall()
        dir_files = [os.path.join(dir_path, dirFile) for dirFile in os.listdir(dir_path)]

        for db_file in db_files:
            db_file = db_file[0]
            if db_file in dir_files:
                self.filewatch_add_path(db_file)
                new_state = self.get_file_state(db_file)
                c.execute('UPDATE FilesTable SET State=? WHERE FilePath=?', (new_state, db_file))
        self.request_sort()

    def filewatch_file_change(self, file_path):
        key_path = self.convert_to_key_path(file_path)
        c = self.db.cursor()
        if get_settings_bool_value(self.settings.value('autoReload', DEFAULT_AUTO_RELOAD)):
            self.is_reloading_file = True
            c.execute('SELECT Name FROM NodesTable LEFT JOIN FilesTable USING(FileId) WHERE KeyPath=?', (key_path, ))
            nodes = c.fetchall()
            for node in [x[0] for x in nodes]:
                node_attr_name = self.supported_format_dict[cmds.nodeType(node)]
                node_attr_value = cmds.getAttr('%s.%s' % (node, node_attr_name))
                cmds.setAttr('%s.%s' % (node, node_attr_name), node_attr_value, type="string")
            self.is_reloading_file = False

        self.filewatch_add_path(file_path)

        new_state = self.get_file_state(file_path)
        c.execute('UPDATE FilesTable SET State=? WHERE KeyPath=?', (new_state, key_path))

        self.request_sort()

    def set_table_view(self, table_view):
        self.table_view = table_view